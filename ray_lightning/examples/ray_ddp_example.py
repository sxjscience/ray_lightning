"""Example using Pytorch Lightning with Pytorch DDP on Ray Accelerator."""
import os
import tempfile

import pytorch_lightning as pl
import torch
from torch.utils.data import random_split, DataLoader
from torchvision.datasets import MNIST
from torchvision import transforms

import ray
from ray import tune
from ray_lightning.tune import TuneReportCallback
from ray_lightning import RayPlugin
from ray_lightning.tests.utils import LightningMNISTClassifier


class MNISTClassifier(LightningMNISTClassifier):
    def __init__(self, config, data_dir=None):
        super().__init__(config, data_dir)
        self.batch_size = config["batch_size"]

    def prepare_data(self):
        self.dataset = MNIST(
            self.data_dir,
            train=True,
            download=True,
            transform=transforms.ToTensor())

    def train_dataloader(self):
        dataset = self.dataset
        train_length = len(dataset)
        dataset_train, _ = random_split(
            dataset, [train_length - 5000, 5000],
            generator=torch.Generator().manual_seed(0))
        loader = DataLoader(
            dataset_train,
            batch_size=self.batch_size,
            num_workers=1,
            drop_last=True,
            pin_memory=True,
        )
        return loader

    def val_dataloader(self):
        dataset = self.dataset
        train_length = len(dataset)
        _, dataset_val = random_split(
            dataset, [train_length - 5000, 5000],
            generator=torch.Generator().manual_seed(0))
        loader = DataLoader(
            dataset_val,
            batch_size=self.batch_size,
            num_workers=1,
            drop_last=True,
            pin_memory=True,
        )
        return loader


def train_mnist(config,
                checkpoint_dir=None,
                data_dir=None,
                num_epochs=10,
                num_workers=1,
                use_gpu=False,
                callbacks=None,
                **trainer_kwargs):
    model = MNISTClassifier(config, data_dir)

    callbacks = callbacks or []

    trainer = pl.Trainer(
        max_epochs=num_epochs,
        gpus=int(use_gpu),
        callbacks=callbacks,
        plugins=[RayPlugin(num_workers=num_workers, use_gpu=use_gpu)],
        **trainer_kwargs)
    trainer.fit(model)


def tune_mnist(data_dir,
               num_samples=10,
               num_epochs=10,
               num_workers=1,
               use_gpu=False,
               **trainer_kwargs):
    config = {
        "layer_1": tune.choice([32, 64, 128]),
        "layer_2": tune.choice([64, 128, 256]),
        "lr": tune.loguniform(1e-4, 1e-1),
        "batch_size": tune.choice([32, 64, 128]),
    }

    # Add Tune callback.
    metrics = {"loss": "ptl/val_loss", "acc": "ptl/val_accuracy"}
    callbacks = [TuneReportCallback(metrics, on="validation_end")]
    trainable = tune.with_parameters(
        train_mnist,
        data_dir=data_dir,
        num_epochs=num_epochs,
        num_workers=num_workers,
        use_gpu=use_gpu,
        callbacks=callbacks,
        **trainer_kwargs)
    analysis = tune.run(
        trainable,
        metric="loss",
        mode="min",
        config=config,
        num_samples=num_samples,
        resources_per_trial={
            "cpu": 1,
            "gpu": int(use_gpu),
            "extra_cpu": num_workers,
            "extra_gpu": num_workers * int(use_gpu)
        },
        name="tune_mnist")

    print("Best hyperparameters found were: ", analysis.best_config)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--num-workers",
        type=int,
        help="Number of training workers to use.",
        default=1)
    parser.add_argument(
        "--use-gpu", action="store_true", help="Use GPU for training.")
    parser.add_argument(
        "--tune",
        action="store_true",
        help="Use Ray Tune for hyperparameter tuning.")
    parser.add_argument(
        "--num-samples",
        type=int,
        default=10,
        help="Number of samples to tune.")
    parser.add_argument(
        "--num-epochs",
        type=int,
        default=10,
        help="Number of epochs to train for.")
    parser.add_argument(
        "--smoke-test", action="store_true", help="Finish quickly for testing")
    parser.add_argument(
        "--address",
        required=False,
        type=str,
        help="the address to use for Ray")
    parser.add_argument(
        "--server-address",
        required=False,
        type=str,
        help="If using Ray Client, the address of the server to connect to. ")
    args, _ = parser.parse_known_args()

    num_epochs = 1 if args.smoke_test else args.num_epochs
    num_workers = 1 if args.smoke_test else args.num_workers
    use_gpu = False if args.smoke_test else args.use_gpu
    num_samples = 1 if args.smoke_test else args.num_samples

    if args.smoke_test:
        ray.init(num_cpus=2)
    elif args.server_address:
        ray.util.connect(args.server_address)
    else:
        ray.init(address=args.address)

    data_dir = os.path.join(tempfile.gettempdir(), "mnist_data_")

    if args.tune:
        tune_mnist(data_dir, num_samples, num_epochs, num_workers, use_gpu)
    else:
        config = {"layer_1": 32, "layer_2": 64, "lr": 1e-1, "batch_size": 32}
        train_mnist(
            config,
            data_dir=data_dir,
            num_epochs=num_epochs,
            num_workers=num_workers,
            use_gpu=use_gpu)