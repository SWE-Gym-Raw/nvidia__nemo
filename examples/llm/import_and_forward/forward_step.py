import torch
from nemo import lightning as nl
from nemo.lightning import io
from nemo.lightning.ckpt_utils import ckpt_to_context_subdir


def run_forward(model_path: str):
    strategy = nl.MegatronStrategy(
        tensor_model_parallel_size=1,
    )
    trainer = nl.Trainer(
        devices=1,
        accelerator="gpu",
        strategy=strategy,
        plugins=nl.MegatronMixedPrecision(precision="bf16-mixed"),
    )
    trainer.strategy._setup_optimizers = False

    fabric = trainer.to_fabric()
    model: io.TrainerContext = io.load_context(path=ckpt_to_context_subdir(model_path), subpath="model")
    model = fabric.load_model(model_path, model)
    model = model.module
    model.eval()

    input_ids = torch.randint(0, 1000, (1, 1024), dtype=torch.long, device=fabric.device)
    position_ids = (
        torch.arange(input_ids.size(1), dtype=torch.long, device=input_ids.device).unsqueeze(0).expand_as(input_ids)
    )
    with torch.no_grad():
        output = model(
            input_ids=input_ids,
            position_ids=position_ids,
            attention_mask=None,
        )
        print(output)


if __name__ == "__main__":
    run_forward("/workspace/starcoder2_3b_nemo2")
