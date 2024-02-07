# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
It's recommended to use manifest input, otherwise the model will perform English ASR with punctuations and capitalizations. 
An example manifest line:
{
    "audio_filepath": "/path/to/audio.wav",  # path to the audio file
    "duration": 10000.0,  # duration of the audio
    "taskname": "asr",  # use "s2t_translation" for AST
    "source_lang": "en",  # ASR only needs `source_lang`
    "target_lang": "de",  # AST needs `target_lang`
    "pnc": yes,  # whether to have PnC output, choices=['yes', 'no'] 
}

Usage:
python speech_to_text_aed_chunked_infer.py \
    model_path=null \
    pretrained_name="nvidia/canary-1b" \
    audio_dir="<remove or path to folder of audio files>" \
    dataset_manifest="<remove or path to manifest>" \
    output_filename="<remove or specify output filename>" \
    chunk_len_in_secs=30.0 \
    batch_size=16
"""

import copy
import glob
import json
import os
from dataclasses import dataclass, is_dataclass
from typing import List, Optional, Union

import pytorch_lightning as pl
import torch
from omegaconf import DictConfig, OmegaConf
from tqdm import tqdm

from nemo.collections.asr.data.audio_to_text_lhotse_prompted import get_canary_prompt_tokens
from nemo.collections.asr.models import EncDecMultiTaskModel
from nemo.collections.asr.parts.submodules.multitask_decoding import MultiTaskDecodingConfig
from nemo.collections.asr.parts.utils import rnnt_utils
from nemo.collections.asr.parts.utils.eval_utils import cal_write_wer
from nemo.collections.asr.parts.utils.streaming_utils import FrameBatchASR
from nemo.collections.asr.parts.utils.transcribe_utils import (
    compute_output_filename,
    get_buffered_pred_feat,
    setup_model,
    wrap_transcription,
    write_transcription,
)
from nemo.collections.common.parts.preprocessing.manifest import get_full_path
from nemo.core.config import hydra_runner
from nemo.utils import logging

can_gpu = torch.cuda.is_available()


@dataclass
class TranscriptionConfig:
    # Required configs
    model_path: Optional[str] = None  # Path to a .nemo file
    pretrained_name: Optional[str] = None  # Name of a pretrained model
    audio_dir: Optional[str] = None  # Path to a directory which contains audio files
    dataset_manifest: Optional[str] = None  # Path to dataset's JSON manifest

    # General configs
    output_filename: Optional[str] = None
    batch_size: int = 32  # number of chunks to process in parallel
    num_workers: int = 0  # unused
    append_pred: bool = False  # Sets mode of work, if True it will add new field transcriptions.
    pred_name_postfix: Optional[str] = None  # If you need to use another model name, rather than standard one.
    random_seed: Optional[int] = None  # seed number going to be used in seed_everything()

    # Set to True to output greedy timestamp information (only supported models)
    compute_timestamps: bool = False

    # Set to True to output language ID information
    compute_langs: bool = False

    # Chunked configs
    chunk_len_in_secs: float = 30.0  # Chunk length in seconds
    model_stride: int = 8  # Model downsampling factor, 8 for Citrinet and FasConformer models and 4 for Conformer models.

    # Decoding strategy for MultitaskAED models
    decoding: MultiTaskDecodingConfig = MultiTaskDecodingConfig()

    # Set `cuda` to int to define CUDA device. If 'None', will look for CUDA
    # device anyway, and do inference on CPU only if CUDA device is not found.
    # If `cuda` is a negative number, inference will be on CPU only.
    cuda: Optional[int] = None
    amp: bool = False
    audio_type: str = "wav"

    # Recompute model transcription, even if the output folder exists with scores.
    overwrite_transcripts: bool = True

    # Config for word / character error rate calculation
    calculate_wer: bool = True
    clean_groundtruth_text: bool = False
    langid: str = "en"  # specify this for convert_num_to_words step in groundtruth cleaning
    use_cer: bool = False


class MultiTaskAEDFrameBatchInfer(FrameBatchASR):
    def get_input_tokens(self, sample: dict):
        if self.asr_model.prompt_format == "canary":
            tokens = get_canary_prompt_tokens(sample, self.asr_model.tokenizer)
        else:
            raise ValueError(f"Unknown prompt format: {self.asr_model.prompt_format}")
        return torch.tensor(tokens, dtype=torch.long, device=self.asr_model.device).unsqueeze(0)  # [1, T]

    def read_audio_file(self, audio_filepath: str, delay, model_stride_in_secs, meta_data):
        self.input_tokens = self.get_input_tokens(meta_data)
        super().read_audio_file(audio_filepath, delay, model_stride_in_secs)

    @torch.no_grad()
    def _get_batch_preds(self, keep_logits=False):
        device = self.asr_model.device
        for batch in iter(self.data_loader):
            feat_signal, feat_signal_len = batch
            feat_signal, feat_signal_len = feat_signal.to(device), feat_signal_len.to(device)
            tokens = self.input_tokens.to(device).repeat(feat_signal.size(0), 1)
            tokens_len = torch.tensor([tokens.size(1)] * tokens.size(0), device=device).long()

            batch_input = (feat_signal, feat_signal_len, tokens, tokens_len)
            predictions = self.asr_model.predict_step(batch_input, has_processed_signal=True)
            self.all_preds.extend(predictions)
            del predictions

    def transcribe(
        self, tokens_per_chunk: Optional[int] = None, delay: Optional[int] = None, keep_logits: bool = False
    ):
        self.infer_logits(keep_logits)

        hypothesis = " ".join(self.all_preds)
        if not keep_logits:
            return hypothesis

        print("keep_logits=True is not supported for MultiTaskAEDFrameBatchInfer. Returning empty logits.")
        return hypothesis, []


def get_buffered_pred_feat(
    asr: MultiTaskAEDFrameBatchInfer,
    preprocessor_cfg: DictConfig,
    model_stride_in_secs: int,
    device: Union[List[int], int],
    manifest: str = None,
    filepaths: List[list] = None,
    delay: float = 0.0,
) -> List[rnnt_utils.Hypothesis]:
    # Create a preprocessor to convert audio samples into raw features,
    # Normalization will be done per buffer in frame_bufferer
    # Do not normalize whatever the model's preprocessor setting is
    preprocessor_cfg.normalize = "None"
    preprocessor = EncDecMultiTaskModel.from_config_dict(preprocessor_cfg)
    preprocessor.to(device)
    hyps = []
    refs = []

    if filepaths and manifest:
        raise ValueError("Please select either filepaths or manifest")
    if filepaths is None and manifest is None:
        raise ValueError("Either filepaths or manifest shoud not be None")

    if filepaths:
        for l in tqdm(filepaths, desc="Sample:"):
            meta = {
                'audio_filepath': l,
                'duration': 100000,
                'source_lang': 'en',
                'taskname': 'asr',
                'target_lang': 'en',
                'pnc': 'yes',
                'answer': 'nothing',
            }
            asr.reset()
            asr.read_audio_file(l, delay, model_stride_in_secs, meta_data=meta)
            hyp = asr.transcribe()
            hyps.append(hyp)
    else:
        with open(manifest, "r", encoding='utf_8') as mfst_f:
            for l in tqdm(mfst_f, desc="Sample:"):
                asr.reset()
                row = json.loads(l.strip())
                if 'text' in row:
                    refs.append(row['text'])
                audio_file = get_full_path(audio_file=row['audio_filepath'], manifest_file=manifest)
                # do not support partial audio
                asr.read_audio_file(audio_file, delay, model_stride_in_secs, meta_data=row)
                hyp = asr.transcribe()
                hyps.append(hyp)

    wrapped_hyps = wrap_transcription(hyps)
    return wrapped_hyps


@hydra_runner(config_name="TranscriptionConfig", schema=TranscriptionConfig)
def main(cfg: TranscriptionConfig) -> TranscriptionConfig:
    logging.info(f'Hydra config: {OmegaConf.to_yaml(cfg)}')
    torch.set_grad_enabled(False)

    for key in cfg:
        cfg[key] = None if cfg[key] == 'None' else cfg[key]

    if is_dataclass(cfg):
        cfg = OmegaConf.structured(cfg)

    if cfg.random_seed:
        pl.seed_everything(cfg.random_seed)

    if cfg.model_path is None and cfg.pretrained_name is None:
        raise ValueError("Both cfg.model_path and cfg.pretrained_name cannot be None!")
    if cfg.audio_dir is None and cfg.dataset_manifest is None:
        raise ValueError("Both cfg.audio_dir and cfg.dataset_manifest cannot be None!")

    filepaths = None
    manifest = cfg.dataset_manifest
    if cfg.audio_dir is not None:
        filepaths = list(glob.glob(os.path.join(cfg.audio_dir, f"**/*.{cfg.audio_type}"), recursive=True))
        manifest = None  # ignore dataset_manifest if audio_dir and dataset_manifest both presents

    # setup GPU
    if cfg.cuda is None:
        if torch.cuda.is_available():
            device = [0]  # use 0th CUDA device
            accelerator = 'gpu'
        else:
            device = 1
            accelerator = 'cpu'
    else:
        device = [cfg.cuda]
        accelerator = 'gpu'
    map_location = torch.device('cuda:{}'.format(device[0]) if accelerator == 'gpu' else 'cpu')
    logging.info(f"Inference will be done on device : {device}")

    asr_model, model_name = setup_model(cfg, map_location)

    model_cfg = copy.deepcopy(asr_model._cfg)
    OmegaConf.set_struct(model_cfg.preprocessor, False)
    # some changes for streaming scenario
    model_cfg.preprocessor.dither = 0.0
    model_cfg.preprocessor.pad_to = 0

    if model_cfg.preprocessor.normalize != "per_feature":
        logging.error("Only EncDecCTCModelBPE models trained with per_feature normalization are supported currently")

    # Disable config overwriting
    OmegaConf.set_struct(model_cfg.preprocessor, True)

    # Compute output filename
    cfg = compute_output_filename(cfg, model_name)

    # if transcripts should not be overwritten, and already exists, skip re-transcription step and return
    if not cfg.overwrite_transcripts and os.path.exists(cfg.output_filename):
        logging.info(
            f"Previous transcripts found at {cfg.output_filename}, and flag `overwrite_transcripts`"
            f"is {cfg.overwrite_transcripts}. Returning without re-transcribing text."
        )
        return cfg

    asr_model.change_decoding_strategy(cfg.decoding)

    asr_model.eval()
    asr_model = asr_model.to(asr_model.device)

    feature_stride = model_cfg.preprocessor['window_stride']
    model_stride_in_secs = feature_stride * cfg.model_stride
    chunk_len = float(cfg.chunk_len_in_secs)

    frame_asr = MultiTaskAEDFrameBatchInfer(
        asr_model=asr_model, frame_len=chunk_len, total_buffer=cfg.chunk_len_in_secs, batch_size=cfg.batch_size,
    )

    hyps = get_buffered_pred_feat(
        frame_asr, model_cfg.preprocessor, model_stride_in_secs, asr_model.device, manifest, filepaths,
    )
    output_filename, pred_text_attr_name = write_transcription(
        hyps, cfg, model_name, filepaths=filepaths, compute_langs=False, compute_timestamps=False
    )
    logging.info(f"Finished writing predictions to {output_filename}!")

    if cfg.calculate_wer:
        output_manifest_w_wer, total_res, _ = cal_write_wer(
            pred_manifest=output_filename,
            pred_text_attr_name=pred_text_attr_name,
            clean_groundtruth_text=cfg.clean_groundtruth_text,
            langid=cfg.langid,
            use_cer=cfg.use_cer,
            output_filename=None,
        )
        if output_manifest_w_wer:
            logging.info(f"Writing prediction and error rate of each sample to {output_manifest_w_wer}!")
            logging.info(f"{total_res}")

    return cfg


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter
