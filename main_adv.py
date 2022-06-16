# Copyright (c) Meta Platforms, Inc. and affiliates.

# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import argparse
import datetime
import numpy as np
import time
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import json
import os
import random

from pathlib import Path

from timm.data.mixup import Mixup
from timm.models import create_model
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.utils import ModelEma
from optim_factory import create_optimizer, LayerDecayValueAssigner

from datasets import build_dataset
from engine import train_one_epoch, evaluate, evaluate_nograd

from utils import NativeScalerWithGradNormCount as NativeScaler
import utils
# import models.convnext
# import models.convnext_isotropic
import models.convnext_timm
import models.swin_timm
import models.deit_vit_timm
import models.poolformer_timm

from attacks.pgd_attack import NoOpAttacker, PGDAttacker


def str2bool(v):
    """
    Converts string to bool type; enables command line 
    arguments in the format of '--arg1 true --arg2 false'
    """
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def get_args_parser():
    parser = argparse.ArgumentParser('ConvNeXt training and evaluation script for image classification', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Per GPU batch size')
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--update_freq', default=1, type=int,
                        help='gradient accumulation steps')

    # Model parameters
    parser.add_argument('--model', default='convnext_tiny', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--pretrained', type=str2bool, default=False, help='Using clean pretrained weights.')
    parser.add_argument('--drop_path', type=float, default=0, metavar='PCT',
                        help='Drop path rate (default: 0.0)')
    parser.add_argument('--input_size', default=224, type=int,
                        help='image input size')
    parser.add_argument('--layer_scale_init_value', default=1e-6, type=float,
                        help="Layer scale initial values")

    # EMA related parameters
    parser.add_argument('--model_ema', type=str2bool, default=False)
    parser.add_argument('--model_ema_decay', type=float, default=0.9999, help='')
    parser.add_argument('--model_ema_force_cpu', type=str2bool, default=False, help='')
    parser.add_argument('--model_ema_eval', type=str2bool, default=False, help='Using ema to eval during training.')

    # Optimization parameters
    parser.add_argument('--opt', default='adamw', type=str, metavar='OPTIMIZER',
                        help='Optimizer (default: "adamw"')
    parser.add_argument('--opt_eps', default=1e-8, type=float, metavar='EPSILON',
                        help='Optimizer Epsilon (default: 1e-8)')
    parser.add_argument('--opt_betas', default=None, type=float, nargs='+', metavar='BETA',
                        help='Optimizer Betas (default: None, use opt default)')
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')
    parser.add_argument('--weight_decay_end', type=float, default=None, help="""Final value of the
        weight decay. We use a cosine schedule for WD and using a larger decay by
        the end of training improves performance for ViTs.""")

    parser.add_argument('--scaled_lr', type=str2bool, default=True)
    parser.add_argument('--warmup_lr', type=float, default=0.0, metavar='LR',
                        help='learning rate (default: 0.0), with total batch size 1024')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                        help='learning rate (default: 0.001), with total batch size 1024')
    parser.add_argument('--layer_decay', type=float, default=1.0)
    # parser.add_argument('--lr', type=float, default=4e-3, metavar='LR',
    #                     help='learning rate (default: 4e-3), with total batch size 4096')
    # parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
    #                     help='lower lr bound for cyclic schedulers that hit 0 (1e-6)')
    parser.add_argument('--min_lr', type=float, default=2.5e-7, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0 (2.5e-7), , with total batch size 1024')
    parser.add_argument('--warmup_epochs', type=int, default=20, metavar='N',
                        help='epochs to warmup LR, if scheduler supports')
    parser.add_argument('--warmup_steps', type=int, default=-1, metavar='N',
                        help='num of steps to warmup LR, will overload warmup_epochs if set > 0')

    # Augmentation parameters
    parser.add_argument('--use_augwarmup', type=str2bool, default=True)
    parser.add_argument('--color_jitter', type=float, default=0.4, metavar='PCT',
                        help='Color jitter factor (default: 0.4)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')
    parser.add_argument('--train_interpolation', type=str, default='bicubic',
                        help='Training interpolation (random, bilinear, bicubic default: "bicubic")')

    # Evaluation parameters
    parser.add_argument('--crop_pct', type=float, default=None)

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', type=str2bool, default=False,
                        help='Do not random erase first (clean) augmentation split')

    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0.8,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=1.0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # * Finetuning params
    parser.add_argument('--finetune', default='',
                        help='finetune from checkpoint')
    parser.add_argument('--head_init_scale', default=1.0, type=float,
                        help='classifier head initial scale, typically adjusted in fine-tuning')
    parser.add_argument('--model_key', default='model|module', type=str,
                        help='which key to load from saved state dict, usually model or model_ema')
    parser.add_argument('--model_prefix', default='', type=str)

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--eval_data_path', default=None, type=str,
                        help='dataset path for evaluation')
    parser.add_argument('--nb_classes', default=1000, type=int,
                        help='number of the classification types for the model definition')
    parser.add_argument('--subset_nclasses', default=1000, type=int,
                        help='num of classes out of 1000 to eval (default: first 1000)')
    parser.add_argument('--imagenet_default_mean_and_std', type=str2bool, default=True)
    parser.add_argument('--image_scale', type=float, default=2.0,
                        help='Range of image after mean std norm, used to scale eps and step for attacker.')
    parser.add_argument('--data_set', default='IMNET', choices=['CIFAR', 'IMNET', 'image_folder'],
                        type=str, help='ImageNet dataset path')
    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default=None,
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)

    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')
    parser.add_argument('--auto_resume', type=str2bool, default=True)
    parser.add_argument('--save_ckpt', type=str2bool, default=True)
    parser.add_argument('--save_ckpt_freq', default=1, type=int)
    parser.add_argument('--save_ckpt_num', default=3, type=int)

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', type=str2bool, default=False,
                        help='Perform evaluation only')
    parser.add_argument('--dist_eval', type=str2bool, default=True,
                        help='Enabling distributed evaluation')
    parser.add_argument('--disable_eval', type=str2bool, default=False,
                        help='Disabling evaluation during training')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', type=str2bool, default=True,
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', type=str2bool, default=False)
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')
    parser.add_argument('--dist_backend', default='nccl', type=str,
                        help='distributed backend')

    parser.add_argument('--use_amp', type=str2bool, default=False,
                        help="Use PyTorch's AMP (Automatic Mixed Precision) or not")
    parser.add_argument('--amp_dtype', default='fp16', type=str, choices=['fp16', 'bf16'],
                        help='datatype for amp autocast')

    # Weights and Biases arguments
    parser.add_argument('--enable_wandb', type=str2bool, default=False,
                        help="enable logging to Weights and Biases")
    parser.add_argument('--project', default='convnext', type=str,
                        help="The name of the W&B project where you're sending the new run.")
    parser.add_argument('--wandb_ckpt', type=str2bool, default=False,
                        help="Save model checkpoints as W&B Artifacts.")
    parser.add_argument('--wandb_mode', default='online', help='online or offine (hpc)')

    #  attacker options
    parser.add_argument('--attack-iter', help='Adversarial attack iteration', type=int, default=0)
    parser.add_argument('--attack-epsilon', help='Adversarial attack maximal perturbation', type=float, default=1.0)
    parser.add_argument('--attack-step-size', help='Adversarial attack step size', type=float, default=1.0)
    parser.add_argument('--prob_start_from_clean', default=0, type=float, help="")

    return parser


def main(args):
    utils.init_distributed_mode(args)
    print(args)
    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    cudnn.benchmark = True

    if args.use_amp:
        print(f'Using amp with dtype: {args.amp_dtype}')
    loss_scaler = NativeScaler()  # if args.use_amp is False, this won't be used

    if args.attack_iter == 0:
        train_attacker = NoOpAttacker()
    else:
        train_attacker = PGDAttacker(num_iter=args.attack_iter,
                                     epsilon=args.attack_epsilon,
                                     step_size=args.attack_step_size,
                                     image_scale=args.image_scale,
                                     prob_start_from_clean=args.prob_start_from_clean,
                                     loss_scaler=loss_scaler,
                                     use_amp=args.use_amp)
    val_attacker = PGDAttacker(num_iter=5,
                               epsilon=4,
                               step_size=1,
                               image_scale=args.image_scale,
                               prob_start_from_clean=0.0,
                               loss_scaler=None,
                               use_amp=False)
    # test attacker uses full precision
    eval_attacker_5 = PGDAttacker(num_iter=5,
                                  epsilon=4,
                                  step_size=1,
                                  image_scale=args.image_scale,
                                  prob_start_from_clean=0.0,
                                  loss_scaler=None,
                                  use_amp=False)
    eval_attacker_10 = PGDAttacker(num_iter=10,
                                   epsilon=4,
                                   step_size=1,
                                   image_scale=args.image_scale,
                                   prob_start_from_clean=0.0,
                                   loss_scaler=None,
                                   use_amp=False)

    if args.use_augwarmup:
        args.aa = 'rand-m1-mstd0.5-inc1'
        dataset_train_1, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m2-mstd0.5-inc1'
        dataset_train_2, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m3-mstd0.5-inc1'
        dataset_train_3, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m4-mstd0.5-inc1'
        dataset_train_4, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m5-mstd0.5-inc1'
        dataset_train_5, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m6-mstd0.5-inc1'
        dataset_train_6, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m7-mstd0.5-inc1'
        dataset_train_7, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m8-mstd0.5-inc1'
        dataset_train_8, _ = build_dataset(is_train=True, args=args)
        args.aa = 'rand-m9-mstd0.5-inc1'
        dataset_train_9, _ = build_dataset(is_train=True, args=args)
    else:
        dataset_train, _ = build_dataset(is_train=True, args=args)
    if args.disable_eval:
        args.dist_eval = False
        dataset_val = None
    else:
        dataset_val, _ = build_dataset(is_train=False, args=args)

    num_tasks = utils.get_world_size()
    global_rank = utils.get_rank()

    if args.use_augwarmup:
        sampler_train_1 = torch.utils.data.DistributedSampler(
            dataset_train_1, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_2 = torch.utils.data.DistributedSampler(
            dataset_train_2, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_3 = torch.utils.data.DistributedSampler(
            dataset_train_3, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_4 = torch.utils.data.DistributedSampler(
            dataset_train_4, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_5 = torch.utils.data.DistributedSampler(
            dataset_train_5, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_6 = torch.utils.data.DistributedSampler(
            dataset_train_6, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_7 = torch.utils.data.DistributedSampler(
            dataset_train_7, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_8 = torch.utils.data.DistributedSampler(
            dataset_train_8, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
        sampler_train_9 = torch.utils.data.DistributedSampler(
            dataset_train_9, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
    else:
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
        )
    if args.dist_eval:
        if len(dataset_val) % num_tasks != 0:
            print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                  'This will slightly alter validation results as extra duplicate entries are added to achieve '
                  'equal num of samples per-process.')
        sampler_val = torch.utils.data.DistributedSampler(
            dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=False)
    else:
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = utils.TensorboardLogger(log_dir=args.log_dir)
    else:
        log_writer = None

    if global_rank == 0 and args.enable_wandb:
        wandb_logger = utils.WandbLogger(args)
    else:
        wandb_logger = None

    if args.use_augwarmup:
        data_loader_train_1 = torch.utils.data.DataLoader(
            dataset_train_1, sampler=sampler_train_1,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
        data_loader_train_2 = torch.utils.data.DataLoader(
            dataset_train_2, sampler=sampler_train_2,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )

        data_loader_train_3 = torch.utils.data.DataLoader(
            dataset_train_3, sampler=sampler_train_3,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
        data_loader_train_4 = torch.utils.data.DataLoader(
            dataset_train_4, sampler=sampler_train_4,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
        data_loader_train_5 = torch.utils.data.DataLoader(
            dataset_train_5, sampler=sampler_train_5,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )

        data_loader_train_6 = torch.utils.data.DataLoader(
            dataset_train_6, sampler=sampler_train_6,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
        data_loader_train_7 = torch.utils.data.DataLoader(
            dataset_train_7, sampler=sampler_train_7,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
        data_loader_train_8 = torch.utils.data.DataLoader(
            dataset_train_8, sampler=sampler_train_8,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )

        data_loader_train_9 = torch.utils.data.DataLoader(
            dataset_train_9, sampler=sampler_train_9,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )
    else:
        data_loader_train = torch.utils.data.DataLoader(
            dataset_train, sampler=sampler_train,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=True,
        )

    if dataset_val is not None:
        data_loader_val = torch.utils.data.DataLoader(
            dataset_val, sampler=sampler_val,
            batch_size=int(0.25 * args.batch_size),
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=False
        )
    else:
        data_loader_val = None

    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    if mixup_active:
        print("Mixup is activated!")
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.nb_classes)

    if args.model.startswith('swin') or args.model.startswith('deit'):
        model = create_model(
            args.model,
            pretrained=args.pretrained,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path
        )
    elif args.model.startswith('convnext'):
        model = create_model(
            args.model,
            pretrained=args.pretrained,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            ls_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale
        )
    elif args.model.startswith('poolformer'):
        model = create_model(
            args.model,
            pretrained=args.pretrained,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            ls_init_value=args.layer_scale_init_value
        )
    else:
        raise AssertionError

    if args.pretrained:
        print(f"loaded {args.model} model from clean pretrained weights")

    if not args.eval:
        model.set_attacker(train_attacker)

    if mixup_active:
        model.set_mixup_fn(True)
    else:
        model.set_mixup_fn(False)

    if args.finetune:
        if args.finetune.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.finetune, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.finetune, map_location='cpu')

        print("Load ckpt from %s" % args.finetune)
        checkpoint_model = None
        for model_key in args.model_key.split('|'):
            if model_key in checkpoint:
                checkpoint_model = checkpoint[model_key]
                print("Load state_dict by model_key = %s" % model_key)
                break
        if checkpoint_model is None:
            checkpoint_model = checkpoint
        state_dict = model.state_dict()
        for k in ['head.weight', 'head.bias']:
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                print(f"Removing key {k} from pretrained checkpoint")
                del checkpoint_model[k]
        utils.load_state_dict(model, checkpoint_model, prefix=args.model_prefix)
    model.to(device)

    model_ema = None
    if args.model_ema:
        # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
        model_ema = ModelEma(
            model,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume='')
        print("Using EMA with decay = %.8f" % args.model_ema_decay)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("Model = %s" % str(model_without_ddp))
    print('number of params:', n_parameters)

    # utils.get_world_size() gives global world size, no need to x num nodes again
    total_batch_size = args.batch_size * args.update_freq * utils.get_world_size()
    if args.use_augwarmup:
        train_len = len(dataset_train_1)
    else:
        train_len = len(dataset_train)
    num_training_steps_per_epoch = train_len // total_batch_size
    print(f'World size: {utils.get_world_size()}')
    print("WARMUP_LR = %.8f" % args.warmup_lr)
    print("LR = %.8f" % args.lr)
    print("MIN_LR = %.8f" % args.min_lr)
    if args.scaled_lr:
        print("Using linear lr scaling")
        linear_scaled_warmup_lr = args.warmup_lr * total_batch_size / 1024.0
        linear_scaled_lr = args.lr * total_batch_size / 1024.0
        linear_scaled_min_lr = args.min_lr * total_batch_size / 1024.0
        args.warmup_lr = linear_scaled_warmup_lr
        args.lr = linear_scaled_lr
        args.min_lr = linear_scaled_min_lr
        print("WARMUP_LR after linear scaling = %.8f" % args.warmup_lr)
        print("LR after linear scaling = %.8f" % args.lr)
        print("MIN_LR after linear scaling = %.8f" % args.min_lr)
    print("Batch size = %d" % total_batch_size)
    print("Update frequent = %d" % args.update_freq)
    print("Number of training examples = %d" % train_len)
    print("Number of training steps per epoch = %d" % num_training_steps_per_epoch)
    print(f'size of train dataset: {train_len}')
    print(f'size of val dataset: {len(dataset_val)}')

    if args.layer_decay < 1.0 or args.layer_decay > 1.0:
        num_layers = 12  # convnext layers divided into 12 parts, each with a different decayed lr value.
        assert args.model in ['convnext_small', 'convnext_base', 'convnext_large', 'convnext_xlarge'], \
            "Layer Decay impl only supports convnext_small/base/large/xlarge"
        assigner = LayerDecayValueAssigner(
            list(args.layer_decay ** (num_layers + 1 - i) for i in range(num_layers + 2)))
    else:
        assigner = None

    if assigner is not None:
        print("Assigned values = %s" % str(assigner.values))

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=False)
        model_without_ddp = model.module

    optimizer = create_optimizer(
        args, model_without_ddp, skip_list=None,
        get_num_layer=assigner.get_layer_id if assigner is not None else None,
        get_layer_scale=assigner.get_scale if assigner is not None else None)

    print("Use Cosine LR scheduler")
    lr_schedule_values = utils.cosine_scheduler(
        args.lr, args.min_lr, args.epochs, num_training_steps_per_epoch,
        warmup_epochs=args.warmup_epochs, start_warmup_value=args.warmup_lr, warmup_steps=args.warmup_steps,
    )

    if args.weight_decay_end is None:
        args.weight_decay_end = args.weight_decay
    wd_schedule_values = utils.cosine_scheduler(
        args.weight_decay, args.weight_decay_end, args.epochs, num_training_steps_per_epoch)
    print("Max WD = %.7f, Min WD = %.7f" % (max(wd_schedule_values), min(wd_schedule_values)))

    if mixup_active:
        # smoothing is handled with mixup label transform
        # mixup and cutmix will change loss fn
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = torch.nn.CrossEntropyLoss()

    print("criterion = %s" % str(criterion))

    utils.auto_load_model(
        args=args, model=model, model_without_ddp=model_without_ddp,
        optimizer=optimizer, loss_scaler=loss_scaler, model_ema=model_ema)

    if args.eval:
        print(f"Test only mode")

        test_stats = evaluate_nograd(data_loader_val, model, device, None, False)
        print(f"Clean Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")
        if args.model_ema_eval:
            test_stats = evaluate_nograd(data_loader_val, model_ema.ema, device, None, False)
            print(f"EMA Clean Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")

        model.module.set_attacker(eval_attacker_5)
        test_stats = evaluate(data_loader_val, model, device, None, False)
        print(f"PGD5 Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")
        if args.model_ema_eval:
            model_ema.ema.set_attacker(eval_attacker_5)
            test_stats = evaluate(data_loader_val, model_ema.ema, device, None, False)
            print(f"EMA PGD5 Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")

        model.module.set_attacker(eval_attacker_10)
        test_stats = evaluate(data_loader_val, model, device, None, False)
        print(f"PGD10 Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")
        if args.model_ema_eval:
            model_ema.ema.set_attacker(eval_attacker_10)
            test_stats = evaluate(data_loader_val, model_ema.ema, device, None, False)
            print(f"EMA PGD10 Test Results | top1: {test_stats['acc1']:.5f}%, top5: {test_stats['acc5']:.5f}%")

        return

    max_accuracy = 0.0
    test_stats = {}
    test_robust_stats = {}
    # if args.model_ema and args.model_ema_eval:
    #     max_accuracy_ema = 0.0

    print(f"Start training from epoch {args.start_epoch} to epoch {args.epochs}")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.use_augwarmup:
            if args.warmup_epochs == 10:
                print('using augwarmup in the first 10e')
                if epoch == 0:
                    data_loader_train = data_loader_train_1
                    args.mixup_prob = 0.5
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 1:
                    data_loader_train = data_loader_train_1
                    args.mixup_prob = 0.6
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 2:
                    data_loader_train = data_loader_train_2
                    args.mixup_prob = 0.7
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 3:
                    data_loader_train = data_loader_train_3
                    args.mixup_prob = 0.8
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 4:
                    data_loader_train = data_loader_train_4
                    args.mixup_prob = 0.9
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 5:
                    data_loader_train = data_loader_train_5
                    args.mixup_prob = 1.0
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 6:
                    data_loader_train = data_loader_train_6
                    args.mixup_prob = 1.0
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.1
                elif epoch == 7:
                    data_loader_train = data_loader_train_7
                    args.mixup_prob = 1.0
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.2
                elif epoch == 8:
                    data_loader_train = data_loader_train_8
                    args.mixup_prob = 0.9
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.3
                elif epoch == 9:
                    data_loader_train = data_loader_train_9
                    args.mixup_prob = 0.95
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.4
                elif epoch >= 10:
                    data_loader_train = data_loader_train_9
                    args.mixup_prob = 1.0
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.5

                if epoch <= 10:
                    print(f'epoch: {epoch}, mixup: {args.mixup}, cutmix: {args.cutmix}, '
                          f'mixup_prob: {args.mixup_prob}, mixup_switch_prob: {args.mixup_switch_prob}')
            else:
                print('using augwarmup in the first 5e')
                if epoch == 0:
                    data_loader_train = data_loader_train_1
                    args.mixup_prob = 0.5
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 1:
                    data_loader_train = data_loader_train_1
                    args.mixup_prob = 0.7
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 2:
                    data_loader_train = data_loader_train_3
                    args.mixup_prob = 0.9
                    args.cutmix = 0.0
                    args.mixup_switch_prob = 0.0
                elif epoch == 3:
                    data_loader_train = data_loader_train_6
                    args.mixup_prob = 1.0
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.1
                elif epoch == 4:
                    data_loader_train = data_loader_train_9
                    args.mixup_prob = 0.9
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.3
                elif epoch >= 5:
                    data_loader_train = data_loader_train_9
                    args.mixup_prob = 1.0
                    args.cutmix = 1.0
                    args.mixup_switch_prob = 0.5

                if epoch <= 5:
                    print(f'epoch: {epoch}, mixup: {args.mixup}, cutmix: {args.cutmix}, '
                          f'mixup_prob: {args.mixup_prob}, mixup_switch_prob: {args.mixup_switch_prob}')
            mixup_fn = Mixup(
                mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
                prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
                label_smoothing=args.smoothing, num_classes=args.nb_classes)

        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)
        if log_writer is not None:
            log_writer.set_step(epoch * num_training_steps_per_epoch * args.update_freq)
        if wandb_logger:
            wandb_logger.set_steps()
        # adv training
        model.module.set_attacker(train_attacker)
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer,
            device, epoch, loss_scaler, args.amp_dtype, args.clip_grad, model_ema, mixup_fn,
            log_writer=log_writer, wandb_logger=wandb_logger, start_steps=epoch * num_training_steps_per_epoch,
            lr_schedule_values=lr_schedule_values, wd_schedule_values=wd_schedule_values,
            num_training_steps_per_epoch=num_training_steps_per_epoch, update_freq=args.update_freq,
            use_amp=args.use_amp
        )
        # save at freq, 1st and last epoch
        epoch_to_save = (epoch + 1) % args.save_ckpt_freq == 0 or epoch + 1 == args.epochs or epoch + 1 == 1
        if args.output_dir and args.save_ckpt:
            if epoch_to_save:
                utils.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, model_ema=model_ema)
        if data_loader_val is not None and epoch_to_save:
            # clean eval
            model.module.set_attacker(NoOpAttacker())
            test_stats = evaluate(data_loader_val, model, device, None, False)
            # test_stats = evaluate(data_loader_val, model, device, args.amp_dtype, args.use_amp)
            print(f"Accuracy of the model on the {len(dataset_val)} clean test images: {test_stats['acc1']:.1f}%")

            # robust eval
            model.module.set_attacker(val_attacker)
            test_robust_stats = evaluate(data_loader_val, model, device, None, False)
            # test_robust_stats = evaluate(data_loader_val, model, device, args.amp_dtype, args.use_amp)
            print(
                f"Accuracy of the model on the {len(dataset_val)} attack test images: {test_robust_stats['acc1']:.1f}%")

            if max_accuracy < test_robust_stats["acc1"]:
                max_accuracy = test_robust_stats["acc1"]
                if args.output_dir and args.save_ckpt:
                    utils.save_model(
                        args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                        loss_scaler=loss_scaler, epoch="best", model_ema=model_ema)
            print(f'Max robust accuracy: {max_accuracy:.2f}%')

            if log_writer is not None:
                log_writer.update(test_acc1=test_stats['acc1'], head="perf", step=epoch)
                log_writer.update(test_acc5=test_stats['acc5'], head="perf", step=epoch)
                log_writer.update(test_loss=test_stats['loss'], head="perf", step=epoch)
                log_writer.update(test_acc1=test_robust_stats['acc1'], head="perf", step=epoch)
                log_writer.update(test_acc5=test_robust_stats['acc5'], head="perf", step=epoch)
                log_writer.update(test_loss=test_robust_stats['loss'], head="perf", step=epoch)

            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         **{f'test_{k}': v for k, v in test_stats.items()},
                         **{f'test_robust_{k}': v for k, v in test_robust_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}
            # # turn off to speed up val 2x
            # # repeat testing routines for EMA, if ema eval is turned on
            # if args.model_ema and args.model_ema_eval:
            #     test_stats_ema = evaluate(data_loader_val, model_ema.ema, device, use_amp=args.use_amp)
            #     print(f"Accuracy of the model EMA on {len(dataset_val)} test images: {test_stats_ema['acc1']:.1f}%")
            #     if max_accuracy_ema < test_stats_ema["acc1"]:
            #         max_accuracy_ema = test_stats_ema["acc1"]
            #         if args.output_dir and args.save_ckpt:
            #             utils.save_model(
            #                 args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
            #                 loss_scaler=loss_scaler, epoch="best-ema", model_ema=model_ema)
            #         print(f'Max EMA accuracy: {max_accuracy_ema:.2f}%')
            #     if log_writer is not None:
            #         log_writer.update(test_acc1_ema=test_stats_ema['acc1'], head="perf", step=epoch)
            #     log_stats.update({**{f'test_{k}_ema': v for k, v in test_stats_ema.items()}})
        else:
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}

        if args.output_dir and utils.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

        if wandb_logger:
            wandb_logger.log_epoch_metrics(log_stats)

    if wandb_logger and args.wandb_ckpt and args.save_ckpt and args.output_dir:
        wandb_logger.log_checkpoints()

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('ConvNeXt training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
