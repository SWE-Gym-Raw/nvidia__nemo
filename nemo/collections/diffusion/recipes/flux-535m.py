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

from typing import Optional

import lightning.pytorch as pl
import nemo_run as run
import torch

from nemo.collections.diffusion.models.flux.model import FluxModelParams, MegatronFluxModel



Name = "flux-535m"

@run.cli.factory(name=NAME)
def model(flux_params=FluxModelParams) -> run.Config[pl.LightningModule]:
    """
    Factory function to create a Flux sample model configuration with only 1 transformer layers.

    Returns:
        run.Config[pl.LightningModule]: Configuration for the Flux sample (535 million) model.

    Examples:
        CLI usage:
            $ nemo llm pretrain model=bert_110m ...

        Python API usage:
            >>> model_config = model(flux_params)
            >>> print(model_config)
    """
    flux_params.t5_params = None
    flux_params.clip_params = None
    flux_params.vae_config = None
    flux_params.flux_config.num_single_layers = 1
    flux_params.flux_config.num_joint_layers = 1
    return MegatronFluxModel(flux_params=flux_params)



@run.cli.factory(target=llm.train, name=NAME)
def unit_test_recipe():
    return run.Partial(
        llm.train,
        model=model(FluxModelParams),
        trainer=run.Config(
            nl.Trainer,
            devices=1,
            num_nodes=int(os.environ.get('SLURM_NNODES', 1)),
            accelerator="gpu",
            strategy=run.Config(
                nl.MegatronStrategy,
                tensor_model_parallel_size=1,
                pipeline_model_parallel_size=1,
                context_parallel_size=1,
                sequence_parallel=False,
                pipeline_dtype=torch.bfloat16,
                ddp=run.Config(
                    DistributedDataParallelConfig,
                    check_for_nan_in_grad=True,
                    grad_reduce_in_fp32=True,
                ),
            ),
            plugins=nl.MegatronMixedPrecision(precision="bf16-mixed"),
            num_sanity_val_steps=0,
            max_steps=10,
            log_every_n_steps=1,
        ),
        log=nl.NeMoLogger(wandb=(WandbLogger() if "WANDB_API_KEY" in os.environ else None)),
        optim=run.Config(
            nl.MegatronOptimizerModule,
            config=run.Config(
                OptimizerConfig,
                lr=1e-4,
                bf16=True,
                use_distributed_optimizer=True,
                weight_decay=0,
            ),
        ),
        tokenizer=None,
        model_transform=None,
    )
