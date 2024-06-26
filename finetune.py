"""
CUDA_VISIBLE_DEVICES=1 python finetune.py --color --save_folder ./saved_models --data_path data/all.list --tar_path data/Xz.tar --tar_in_memory --process_ahead --batch_size 32 --epochs 10 --train_module encoder --decoder_type decoder
CUDA_VISIBLE_DEVICES=2 python finetune.py --color --save_folder ./saved_models --data_path data/all.list --tar_path data/Xz.tar --tar_in_memory --process_ahead --batch_size 32 --epochs 10 --train_module encoder --decoder_type dvae
CUDA_VISIBLE_DEVICES=3 python finetune.py --color --save_folder ./saved_models --data_path data/Xz/Bekki.list --tar_path data/Xz.tar --tar_in_memory --process_ahead --batch_size 16 --epochs 10 --train_module gpt_speaker --gpt_lora --decoder_encoder_path ./saved_models/decoder_encoder.pth --dvae_encoder_path ./saved_models/dvae_encoder.pth
"""

import argparse
import functools
import os
from enum import StrEnum

import torch.utils.data
import torch.nn
import transformers
from transformers.trainer_pt_utils import LabelSmoother
import numpy as np

import ChatTTS
import ChatTTS.model.gpt
import ChatTTS.model.dvae
from utils.dataset import XzListTar, AudioFolder, AudioCollator
from utils.model import quantize
from utils.encoder import DVAEEncoder, get_encoder_config
from utils.logger import MetricLogger
from utils.output import ansi, get_ansi_len, output_iter

IGNORE_TOKEN_ID = LabelSmoother.ignore_index


class TrainModule(StrEnum):
    GPT_SPEAKER = 'gpt_speaker'
    GPT = 'gpt'
    SPEAKER = 'speaker'

    AUTOENCODER = 'autoencoder'
    ENCODER = 'encoder'
    DECODER = 'decoder'


class DecoderType(StrEnum):
    DECODER = 'decoder'
    DVAE = 'dvae'


def train_autoencoder(
        chat: ChatTTS.Chat,
        dataset: AudioFolder,
        encoder: DVAEEncoder,
        decoder: ChatTTS.model.dvae.DVAE,
        train_module: TrainModule = TrainModule.AUTOENCODER,
        batch_size: int = 16,
        epochs: int = 10,
):
    tokenizer: transformers.PreTrainedTokenizer = chat.pretrain_models['tokenizer']
    encoder: DVAEEncoder = DVAEEncoder(
        **get_encoder_config(decoder.decoder),
    ).to(device=dataset.device)

    match train_module:
        case TrainModule.AUTOENCODER:
            encoder.train().requires_grad_()
            decoder.train().requires_grad_()
            train_params = list(encoder.parameters()) + list(decoder.parameters())
        case TrainModule.ENCODER:
            encoder.train().requires_grad_()
            decoder.eval().requires_grad_(False)
            train_params = list(encoder.parameters())
        case TrainModule.DECODER:
            encoder.eval().requires_grad_(False)
            decoder.train().requires_grad_()
            train_params = list(decoder.parameters())

    loss_fn = torch.nn.MSELoss()
    lr = 1e-3 if train_module == TrainModule.ENCODER else 1e-4
    optimizer = torch.optim.AdamW(train_params, lr=lr, betas=[0.8, 0.99], eps=1e-6)
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, epochs, 1e-7)
    # lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.999999)

    vq_layer = decoder.vq_layer
    decoder.vq_layer = None
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=AudioCollator(text_pad=tokenizer.pad_token_id),
        # num_workers=4,
    )
    logger = MetricLogger()
    logger.create_meters(loss=None)
    for _epoch in range(epochs):
        _epoch += 1
        logger.reset()
        header: str = '{blue_light}{0}: {1}{reset}'.format(
            'Epoch', output_iter(_epoch, epochs), **ansi)
        header = header.ljust(max(len('Epoch'), 30) + get_ansi_len(header))
        iterator = logger.log_every(loader, header=header, tqdm_header='Batch')
        for batch in iterator:
            audio_mel_specs: torch.Tensor = batch['audio_mel_specs']  # (batch_size, audio_len*2, 100)
            audio_attention_mask: torch.Tensor = batch['audio_attention_mask']  # (batch_size, audio_len)
            mel_attention_mask = audio_attention_mask.unsqueeze(-1).repeat(1, 1, 2).flatten(1)  # (batch_size, audio_len*2)

            # (batch_size, audio_len, audio_dim)
            audio_latents = encoder(audio_mel_specs, audio_attention_mask) * audio_attention_mask.unsqueeze(-1)
            # (batch_size, audio_len*2, 100)
            if vq_layer is not None:
                audio_latents, _ = quantize(vq_layer.quantizer, audio_latents)  # (batch_size, audio_len, num_vq)
            gen_mel_specs: torch.Tensor = decoder(audio_latents.transpose(1, 2)).transpose(1, 2) * mel_attention_mask.unsqueeze(-1)

            loss: torch.Tensor = loss_fn(gen_mel_specs, audio_mel_specs)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(train_params, 1.0)
            optimizer.step()
            logger.meters['loss'].update(loss.item(), n=len(audio_attention_mask))
        lr_scheduler.step()
    optimizer.zero_grad()
    decoder.vq_layer = vq_layer


def train_gpt(
    chat: ChatTTS.Chat,
    dataset: AudioFolder,
    decoder_encoder: DVAEEncoder,
    dvae_encoder: DVAEEncoder,
    train_module: TrainModule = TrainModule.GPT_SPEAKER,
    batch_size: int = 16,
    epochs: int = 10,
    train_text: bool = True,
    speaker_embeds: dict[str, torch.Tensor] = {},
) -> dict[str, torch.Tensor]:
    tokenizer: transformers.PreTrainedTokenizer = chat.pretrain_models['tokenizer']

    decoder_decoder: ChatTTS.model.dvae.DVAE = chat.pretrain_models['decoder']
    decoder_decoder.eval().requires_grad_(False)
    # decoder_encoder: DVAEEncoder = DVAEEncoder(
    #     **get_encoder_config(decoder_decoder.decoder),
    # )
    decoder_encoder.to(device=dataset.device).eval().requires_grad_(False)

    dvae_decoder: ChatTTS.model.dvae.DVAE = chat.pretrain_models['dvae']
    dvae_decoder.eval().requires_grad_(False)
    # dvae_encoder: DVAEEncoder = DVAEEncoder(
    #     **get_encoder_config(dvae_decoder.decoder),
    # )
    dvae_encoder.to(device=dataset.device).eval().requires_grad_(False)

    gpt: ChatTTS.model.gpt.GPT_wrapper = chat.pretrain_models['gpt']
    if train_module == TrainModule.SPEAKER:
        gpt.eval().requires_grad_(False)
    else:
        gpt.train().requires_grad_()

    speaker_embeds = {
        speaker: torch.randn(
            768,
            device=dataset.device,
            requires_grad=train_module in [TrainModule.GPT_SPEAKER, TrainModule.SPEAKER],
        ) for speaker in dataset.speakers
    } | speaker_embeds
    for speaker_embed in speaker_embeds.values():
        std, mean = chat.pretrain_models['spk_stat'].chunk(2)
        speaker_embed.data = speaker_embed.data * std + mean
    SPEAKER_TOKEN_ID: int = tokenizer.convert_tokens_to_ids('[spk_emb]')
    AUDIO_EOS_TOKEN_ID: int = 0
    # AUDIO_EOS_TOKEN_ID: int = tokenizer.convert_tokens_to_ids('[Etts]')
    AUDIO_PAD_TOKEN_ID: int = AUDIO_EOS_TOKEN_ID

    match train_module:
        case TrainModule.GPT_SPEAKER:
            train_params = list(gpt.parameters()) + list(speaker_embeds.values())
            optimizer = torch.optim.Adam(gpt.parameters(), lr=1e-3, weight_decay=0, betas=[0.9, 0.95], eps=1e-5)
            optimizer.add_param_group({'params': speaker_embeds.values(), 'lr': 1e-1})
        case TrainModule.GPT:
            train_params = list(gpt.parameters())
        case TrainModule.SPEAKER:
            train_params = list(speaker_embeds.values())
            optimizer = torch.optim.Adam(train_params, lr=1e-2, weight_decay=0, betas=[0.9, 0.95], eps=1e-5)

    loss_fn = torch.nn.CrossEntropyLoss()
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, epochs, 1e-7)
    # lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=functools.partial())

    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=AudioCollator(text_pad=tokenizer.pad_token_id),
        # num_workers=4,
    )
    logger = MetricLogger()
    logger.create_meters(loss=None, mse_loss=None, audio_loss=None, text_loss=None)
    for _epoch in range(epochs):
        _epoch += 1
        logger.reset()
        header: str = '{blue_light}{0}: {1}{reset}'.format(
            'Epoch', output_iter(_epoch, epochs), **ansi)
        header = header.ljust(max(len('Epoch'), 30) + get_ansi_len(header))
        iterator = logger.log_every(loader, header=header, tqdm_header='Batch')
        for batch in iterator:
            speakers: list[str] = batch['speaker']  # (batch_size,)
            text_input_ids: torch.Tensor = batch['text_input_ids']   # (batch_size, text_len)
            text_attention_mask: torch.Tensor = batch['text_attention_mask']   # (batch_size, text_len)
            audio_mel_specs: torch.Tensor = batch['audio_mel_specs']   # (batch_size, audio_len*2, 100)
            audio_attention_mask: torch.Tensor = batch['audio_attention_mask']   # (batch_size, audio_len)

            batch_size, text_len = text_attention_mask.size()

            dvae_audio_latents = dvae_encoder(audio_mel_specs, audio_attention_mask)  # (batch_size, audio_len, audio_dim=1024)
            _, dvae_audio_input_ids = quantize(dvae_decoder.vq_layer.quantizer, dvae_audio_latents)  # (batch_size, audio_len, num_vq)
            dvae_audio_input_ids[~audio_attention_mask.bool()] = AUDIO_PAD_TOKEN_ID

            # add audio eos token
            extended_audio_attention_mask = torch.cat(
                [
                    audio_attention_mask,
                    torch.zeros(
                        (batch_size, 1),
                        dtype=audio_attention_mask.dtype,
                        device=audio_attention_mask.device,
                    ),
                ],
                dim=1,
            )  # (batch_size, audio_len+1)
            extended_audio_input_ids = torch.cat(
                [
                    dvae_audio_input_ids,
                    AUDIO_PAD_TOKEN_ID * torch.ones(
                        (batch_size, 1, gpt.num_vq),
                        dtype=dvae_audio_input_ids.dtype,
                        device=dvae_audio_input_ids.device,
                    ),
                ],
                dim=1,
            )  # (batch_size, audio_len+1, num_vq)
            indices = audio_attention_mask.int().sum(dim=1)  # (batch_size,)
            for i in range(batch_size):
                extended_audio_attention_mask[i, indices[i]] = 1
                extended_audio_input_ids[i, indices[i]] = AUDIO_EOS_TOKEN_ID

            # combine text and audio
            input_ids = torch.cat(   # (batch_size, text_len + audio_len + 1, num_vq)
                [
                    text_input_ids.unsqueeze(-1).repeat(1, 1, gpt.num_vq),   # (batch_size, text_len, num_vq)
                    extended_audio_input_ids,   # (batch_size, audio_len, num_vq)
                ],
                dim=1,
            )
            attention_mask = torch.cat(   # (batch_size, text_len + audio_len + 1)
                [text_attention_mask, extended_audio_attention_mask],
                dim=1,
            )
            text_mask = torch.cat(   # (batch_size, text_len + audio_len + 1)
                [
                    torch.ones_like(text_attention_mask, dtype=bool),
                    torch.zeros_like(extended_audio_attention_mask, dtype=bool),
                ],
                dim=1,
            )
            # set labels
            labels = input_ids.clone()   # (batch_size, text_len + audio_len + 1, num_vq)
            labels[~attention_mask.bool()] = IGNORE_TOKEN_ID

            # (batch_size, text_len + audio_len, 768)
            inputs_embeds = gpt.get_emb(input_ids=input_ids, text_mask=text_mask)

            # (batch_size, text_len + audio_len)
            indices = torch.all(input_ids == SPEAKER_TOKEN_ID, dim=-1)
            for i, speaker in enumerate(speakers):
                inputs_embeds[i, indices[i]] = torch.nn.functional.normalize(
                    speaker_embeds[speaker].to(dtype=inputs_embeds.dtype),
                    p=2.0,
                    dim=-1,
                    eps=1e-12,
                ).unsqueeze(0)
            outputs = gpt.gpt.forward(inputs_embeds=inputs_embeds, attention_mask=attention_mask)
            hidden_states = outputs.last_hidden_state  # (batch_size, text_len + audio_len + 1, 768)
            text_hidden_states = hidden_states[:, :text_len-1]  # (batch_size, text_len-1, 768)
            audio_hidden_states = hidden_states[:, text_len-1:-1]  # (batch_size, audio_len+1, 768)

            audio_logits = torch.stack(
                [gpt.head_code[i](audio_hidden_states) for i in range(gpt.num_vq)],
                dim=2,
            )  # (batch_size, audio_len+1, num_vq, num_class_audio)
            audio_loss: torch.Tensor = loss_fn(audio_logits.flatten(0, 2), labels[:, text_len:].flatten(0, 2))
            loss: torch.Tensor = audio_loss
            if train_text:
                text_logits: torch.Tensor = gpt.head_text(text_hidden_states)  # (batch_size, text_len-1, num_class_text)
                text_loss: torch.Tensor = loss_fn(text_logits.flatten(0, 1), labels[:, 1:text_len, 0].flatten(0, 1))
                loss += text_loss
                logger.meters['text_loss'].update(text_loss.item(), n=batch_size)

            gpt_gen_mel_specs = decoder_decoder(audio_hidden_states[:, :-1].transpose(1, 2)).transpose(1, 2)
            mse_loss = torch.nn.functional.mse_loss(
                gpt_gen_mel_specs,
                audio_mel_specs,
            )
            loss += 0.01 * mse_loss

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(train_params, 1.0)
            optimizer.step()
            logger.meters['loss'].update(loss.item(), n=batch_size)
            logger.meters['mse_loss'].update(mse_loss.item(), n=batch_size)
            logger.meters['audio_loss'].update(audio_loss.item(), n=batch_size)
        lr_scheduler.step()
    optimizer.zero_grad()
    return speaker_embeds


def main():
    parser = argparse.ArgumentParser(description='ChatTTS demo Launch')
    parser.add_argument('--local_path', type=str, default=None, help='the local_path if need')
    parser.add_argument('--data_path', type=str, default='dummy_data/xz_list_style/speaker_A.list', help='the data_path to json/list file')
    parser.add_argument('--tar_path', type=str, help='the tarball path with wavs')
    parser.add_argument('--tar_in_memory', action='store_true', help='load tarball in memory')
    parser.add_argument('--process_ahead', action='store_true', help='process all data ahead during dataset initialization')
    parser.add_argument(
        '--train_module', type=str, default='gpt',
        choices=['gpt_speaker', 'gpt', 'speaker', 'autoencoder', 'encoder', 'decoder'],
    )
    parser.add_argument(
        '--decoder_type', type=str, default='decoder',
        choices=['decoder', 'dvae'],
    )
    parser.add_argument('--train_text', action='store_true', help='train text loss')
    parser.add_argument('--gpt_lora', action='store_true', help='train gpt with lora')
    # parser.add_argument('--gpt_kbit', type=int, default=16, help='train gpt with kbit')
    parser.add_argument('--decoder_encoder_path', type=str)
    parser.add_argument('--decoder_decoder_path', type=str)
    parser.add_argument('--dvae_encoder_path', type=str)
    parser.add_argument('--dvae_decoder_path', type=str)
    parser.add_argument('--gpt_path', type=str)
    parser.add_argument('--speaker_embeds_path', type=str)
    parser.add_argument('--save_folder', type=str, default='./')
    parser.add_argument('--batch_size', type=int, default=16)
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--color', action='store_true', help='colorful output')
    args = parser.parse_args()
    local_path: str | None = args.local_path
    data_path: str = args.data_path
    tar_path: str | None = args.tar_path
    tar_in_memory: bool = args.tar_in_memory
    process_ahead: bool = args.process_ahead
    train_module: TrainModule = args.train_module
    decoder_type: DecoderType = args.decoder_type
    train_text: bool = args.train_text
    gpt_lora: bool = args.gpt_lora
    # gpt_kbit: int = args.gpt_kbit
    save_folder: str = args.save_folder
    batch_size: int = args.batch_size
    epochs: int = args.epochs

    decoder_encoder_path: str = args.decoder_encoder_path
    decoder_decoder_path: str = args.decoder_decoder_path
    dvae_encoder_path: str = args.dvae_encoder_path
    dvae_decoder_path: str = args.dvae_decoder_path
    gpt_path: str = args.gpt_path
    speaker_embeds_path: str = args.speaker_embeds_path

    chat = ChatTTS.Chat()
    if local_path is None:
        chat.load_models()
    else:
        print('local model path:', local_path)
        chat.load_models('local', local_path=local_path)

    dataset = XzListTar(
        root=data_path,
        tokenizer=chat.pretrain_models['tokenizer'],
        vocos_model=chat.pretrain_models['vocos'],
        tar_path=tar_path,
        tar_in_memory=tar_in_memory,
        process_ahead=process_ahead,
        # device=None,
        # speakers=None,  # set(['speaker_A', 'speaker_B'])
    )

    decoder_decoder: ChatTTS.model.dvae.DVAE = chat.pretrain_models['decoder']
    decoder_encoder: DVAEEncoder = DVAEEncoder(
        **get_encoder_config(decoder_decoder.decoder),
    )
    dvae_decoder: ChatTTS.model.dvae.DVAE = chat.pretrain_models['dvae']
    dvae_encoder: DVAEEncoder = DVAEEncoder(
        **get_encoder_config(dvae_decoder.decoder),
    )
    gpt: ChatTTS.model.gpt.GPT_wrapper = chat.pretrain_models['gpt']

    # load pretrained models
    if decoder_encoder_path is not None:
        decoder_encoder.load_state_dict(torch.load(decoder_encoder_path, map_location=dataset.device))
    if decoder_decoder_path is not None:
        decoder_decoder.load_state_dict(torch.load(decoder_decoder_path, map_location=dataset.device))
    if dvae_encoder_path is not None:
        dvae_encoder.load_state_dict(torch.load(dvae_encoder_path, map_location=dataset.device))
    if dvae_decoder_path is not None:
        dvae_decoder.load_state_dict(torch.load(dvae_decoder_path, map_location=dataset.device))
    if gpt_path is not None:
        gpt.load_state_dict(torch.load(gpt_path, map_location=dataset.device))
    if speaker_embeds_path is None:
        speaker_embeds: dict[str, torch.Tensor] = {}
    else:
        np_speaker_embeds: dict[str, np.ndarray] = np.load(speaker_embeds_path)
        speaker_embeds = {
            speaker: torch.from_numpy(speaker_embed).to(dataset.device)
            for speaker, speaker_embed in np_speaker_embeds.items()
        }

    if train_module in [TrainModule.GPT_SPEAKER, TrainModule.GPT]:
        gpt: ChatTTS.model.gpt.GPT_wrapper = chat.pretrain_models['gpt']
        if gpt_lora:
            import peft
            # match gpt_kbit:
            #     case 4:
            #         quantization_config = transformers.BitsAndBytesConfig(
            #             load_in_4bit=True,
            #             bnb_4bit_quant_type="nf4",
            #             bnb_4bit_use_double_quant=True,
            #             bnb_4bit_compute_dtype=torch.bfloat16,
            #         )
            #     case 8:
            #         quantization_config = transformers.BitsAndBytesConfig(
            #             load_in_8bit=True,
            #     )
            # gpt.gpt = transformers.LlamaModel.from_pretrained()
            # peft.prepare_model_for_gpt_kbit_training(gpt.gpt)
            lora_config = peft.LoraConfig(r=8, lora_alpha=16)
            gpt.gpt = peft.get_peft_model(gpt.gpt, lora_config)

    if train_module in [TrainModule.GPT_SPEAKER, TrainModule.GPT, TrainModule.SPEAKER]:
        train = functools.partial(
            train_gpt,
            decoder_encoder=decoder_encoder,
            dvae_encoder=dvae_encoder,
            train_text=train_text,
            speaker_embeds=speaker_embeds,
        )
    else:
        if decoder_type == DecoderType.DECODER:
            encoder = decoder_encoder
            decoder = decoder_decoder
        else:
            encoder = dvae_encoder
            decoder = dvae_decoder
        train = functools.partial(train_autoencoder, encoder=encoder, decoder=decoder)
    speaker_embeds = train(chat=chat, dataset=dataset, train_module=train_module, batch_size=batch_size, epochs=epochs)

    if not os.path.isdir(save_folder):
        os.makedirs(save_folder)
    gpt_save_path = os.path.join(save_folder, 'gpt.pth')
    speaker_embeds_save_path = os.path.join(save_folder, 'speaker_embeds.npz')
    decoder_encoder_save_path = os.path.join(save_folder, 'decoder_encoder.pth')
    decoder_decoder_save_path = os.path.join(save_folder, 'decoder_decoder.pth')
    dvae_encoder_save_path = os.path.join(save_folder, 'dvae_encoder.pth')
    dvae_decoder_save_path = os.path.join(save_folder, 'dvae_decoder.pth')
    if train_module in [TrainModule.GPT_SPEAKER, TrainModule.GPT] and gpt_lora:
        gpt.gpt = gpt.gpt.merge_and_unload()
    if speaker_embeds is not None:
        np_speaker_embeds = {speaker: speaker_embed.detach().cpu().numpy() for speaker, speaker_embed in speaker_embeds.items()}
    match train_module:
        case TrainModule.GPT_SPEAKER:
            torch.save(gpt.state_dict(), gpt_save_path)
            np.savez(speaker_embeds_save_path, **np_speaker_embeds)
        case TrainModule.GPT:
            torch.save(gpt.state_dict(), gpt_save_path)
        case TrainModule.SPEAKER:
            np.savez(speaker_embeds_save_path, **np_speaker_embeds)
        case TrainModule.AUTOENCODER:
            if decoder_type == DecoderType.DECODER:
                torch.save(decoder_encoder.state_dict(), decoder_encoder_save_path)
                torch.save(decoder_decoder.state_dict(), decoder_decoder_save_path)
            else:
                torch.save(dvae_encoder.state_dict(), dvae_encoder_save_path)
                torch.save(dvae_decoder.state_dict(), dvae_decoder_save_path)
        case TrainModule.ENCODER:
            if decoder_type == DecoderType.DECODER:
                torch.save(decoder_encoder.state_dict(), decoder_encoder_save_path)
            else:
                torch.save(dvae_encoder.state_dict(), dvae_encoder_save_path)
        case TrainModule.DECODER:
            if decoder_type == DecoderType.DECODER:
                torch.save(decoder_decoder.state_dict(), decoder_decoder_save_path)
            else:
                torch.save(dvae_decoder.state_dict(), dvae_decoder_save_path)
    print('save models to:', save_folder)


if __name__ == '__main__':
    main()
