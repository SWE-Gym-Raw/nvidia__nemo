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

import pytest
import torch

import nemo.lightning as nl
from nemo.collections.llm.bert.data.pre_training import BERTPreTrainingDataModule
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer

DATA_PATH = "/home/TestData/nlp/megatron_bert/data/bert/simple_wiki_gpt_preproc_text_sentence"
VOCAB_PATH = "/home/TestData/nlp/megatron_bert/data/bert/vocab.json"


@pytest.fixture
def tokenizer():
    return get_nmt_tokenizer(
        "megatron",
        "BertWordPieceLowerCase",
        vocab_file=VOCAB_PATH,
    )


@pytest.fixture
def trainer():
    return nl.Trainer(
        accelerator="cpu",
        max_steps=1,
    )


@pytest.fixture(scope='session', autouse=True)
def setup_once():
    # This will run only once before any tests
    torch.distributed.init_process_group(world_size=1, rank=0)


def test_single_data_distribution(tokenizer, trainer):
    data = BERTPreTrainingDataModule(
        paths=[DATA_PATH],
        seq_length=512,
        micro_batch_size=2,
        global_batch_size=2,
        tokenizer=tokenizer,
    )
    data.trainer = trainer

    ## AssertioneError because we are trying to do eval on the whole
    ## dataset with just a single distribution
    with pytest.raises(AssertionError):
        data.setup(stage="dummy")

    trainer.limit_val_batches = 5
    ## this should succeed
    data.setup(stage="dummy")


def test_multiple_data_distributions(tokenizer, trainer):
    data = BERTPreTrainingDataModule(
        paths={
            "train": ['1', DATA_PATH],
            "validation": [DATA_PATH],
            "test": ['1', DATA_PATH],
        },
        seq_length=512,
        micro_batch_size=2,
        global_batch_size=2,
        tokenizer=tokenizer,
    )
    data.trainer = trainer

    ## this should succeed
    data.setup(stage="dummy")
