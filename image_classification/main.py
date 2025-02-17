import argparse
import random

import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import actnn
from actnn import config, QScheme, QModule

try:
    # from apex.parallel import DistributedDataParallel as DDP
    from torch.nn.parallel import DistributedDataParallel as DDP
    from apex.fp16_utils import *
    from apex import amp
except ImportError:
    raise ImportError("Please install apex from https://www.github.com/nvidia/apex to run this example.")

from image_classification.smoothing import LabelSmoothing
from image_classification.mixup import NLLMultiLabelSmooth, MixUpWrapper
from image_classification.dataloaders import *
from image_classification.training import *
from image_classification.utils import *


def add_parser_arguments(parser):
    model_names = models.resnet_versions.keys()
    model_configs = models.resnet_configs.keys()

    parser.add_argument('data', metavar='DIR',
                        help='path to dataset')
    parser.add_argument('--dataset', type=str, default='imagenet')

    parser.add_argument('--data-backend', metavar='BACKEND', default='pytorch',
                        choices=DATA_BACKEND_CHOICES)

    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet50',
                        choices=model_names,
                        help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet50)')

    parser.add_argument('--model-config', '-c', metavar='CONF', default='fanin',
                        choices=model_configs,
                        help='model configs: ' +
                        ' | '.join(model_configs) + '(default: classic)')

    parser.add_argument('-j', '--workers', default=5, type=int, metavar='N',
                        help='number of data loading workers (default: 5)')
    parser.add_argument('--num-classes', default=1000, type=int, metavar='N',
                        help='number of classes (default: 1000)')
    parser.add_argument('--epochs', default=90, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('-b', '--batch-size', default=64, type=int,
                        metavar='N', help='mini-batch size (default: 256) per gpu')

    parser.add_argument('--optimizer-batch-size', default=-1, type=int,
                        metavar='N', help='size of a total batch size, for simulating bigger batches')

    parser.add_argument('--lr', '--learning-rate', default=0.512, type=float,
                        metavar='LR', help='initial learning rate')
    parser.add_argument('--lr-schedule', default='cosine', type=str, metavar='SCHEDULE', choices=['step','linear','cosine'])

    parser.add_argument('--warmup', default=4, type=int,
                        metavar='E', help='number of warmup epochs')

    parser.add_argument('--label-smoothing', default=0.1, type=float,
                        metavar='S', help='label smoothing')
    parser.add_argument('--mixup', default=0.0, type=float,
                        metavar='ALPHA', help='mixup alpha')

    parser.add_argument('--momentum', default=0.875, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--weight-decay', '--wd', default=3.0517578125e-05, type=float,
                        metavar='W', help='weight decay (default: 1e-4)')
    parser.add_argument('--bn-weight-decay', action='store_true',
                        help='use weight_decay on batch normalization learnable parameters, default: false)')
    parser.add_argument('--nesterov', action='store_true',
                        help='use nesterov momentum, default: false)')

    parser.add_argument('--print-freq', '-p', default=100, type=int,
                        metavar='N', help='print frequency (default: 10)')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('--resume2', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('--pretrained-weights', default='', type=str, metavar='PATH',
                        help='load weights from here')

    parser.add_argument('--fp16', action='store_true',
                        help='Run model fp16 mode.')
    parser.add_argument('--static-loss-scale', type=float, default=1,
                        help='Static loss scale, positive power of 2 values can improve fp16 convergence.')
    parser.add_argument('--dynamic-loss-scale', action='store_true',
                        help='Use dynamic loss scaling.  If supplied, this argument supersedes ' +
                        '--static-loss-scale.')
    parser.add_argument('--prof', type=int, default=-1,
                        help='Run only N iterations')
    parser.add_argument('--amp', action='store_true',
                        help='Run model AMP (automatic mixed precision) mode.')

    parser.add_argument("--local_rank", default=0, type=int)

    parser.add_argument('--seed', default=None, type=int,
                        help='random seed used for np and pytorch')

    parser.add_argument('--gather-checkpoints', action='store_true',
                        help='Gather checkpoints throughout the training')

    parser.add_argument('--raport-file', default='raport.json', type=str,
                        help='file in which to store JSON experiment raport')

    parser.add_argument('--final-weights', default='model.pth.tar', type=str,
                        help='file in which to store final model weights')

    parser.add_argument('--evaluate', action='store_true', help='evaluate checkpoint/model')
    parser.add_argument('--training-only', action='store_true', help='do not evaluate')

    parser.add_argument('--no-checkpoints', action='store_false', dest='save_checkpoints')

    parser.add_argument('--workspace', type=str, default='./')

    def str2bool(v):
        if isinstance(v, bool):
            return v
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Boolean value expected.')

    parser.add_argument('--ca', type=str2bool, default=True, help='compress activation')
    parser.add_argument('--sq', type=str2bool, default=True, help='stochastic quantization')
    parser.add_argument('--cabits', type=float, default=8, help='activation number of bits')
    parser.add_argument('--qat', type=int, default=8, help='quantization aware training bits')
    parser.add_argument('--ibits', type=int, default=8, help='Initial precision for the allocation algorithm')
    parser.add_argument('--actnn-level', type=str, default='L3', help='Optimization level for ActNN')
    # parser.add_argument('--pergroup', type=str2bool, default=True, help='Per-group range')
    parser.add_argument('--groupsize', type=int, default=256, help='Size for each quantization group')
    # parser.add_argument('--perlayer', type=str2bool, default=True, help='Per layer quantization')
    parser.add_argument('--usegradient', type=str2bool, default=False, help='Using gradient information for persample')


def main(args):
    actnn.set_optimization_level(args.actnn_level)
    if args.actnn_level in {'L1', 'L2'}:
        config.activation_compression_bits = [int(args.cabits)]
        config.initial_bits = int(args.cabits)
    else:
        config.activation_compression_bits = [args.cabits]

    # Note: we use these flags for debugging. Users may simply use "actnn.set_optimization_level"
    # config.compress_activation = args.ca
    config.stochastic = args.sq
    config.qat = args.qat
    config.use_gradient = args.usegradient
    config.group_size = args.groupsize

    exp_start_time = time.time()
    global best_prec1
    best_prec1 = 0

    args.distributed = False
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        args.gpu = args.local_rank % torch.cuda.device_count()
        torch.cuda.set_device(args.gpu)
        dist.init_process_group(backend='nccl', init_method='env://')
        args.world_size = torch.distributed.get_world_size()

    if args.amp and args.fp16:
        print("Please use only one of the --fp16/--amp flags")
        exit(1)

    if args.seed is not None:
        print("Using seed = {}".format(args.seed))
        torch.manual_seed(args.seed + args.local_rank)
        torch.cuda.manual_seed(args.seed + args.local_rank)
        np.random.seed(seed=args.seed + args.local_rank)
        random.seed(args.seed + args.local_rank)

        def _worker_init_fn(id):
            np.random.seed(seed=args.seed + args.local_rank + id)
            random.seed(args.seed + args.local_rank + id)
    else:
        def _worker_init_fn(id):
            pass

    if args.fp16:
        assert torch.backends.cudnn.enabled, "fp16 mode requires cudnn backend to be enabled."

    if args.static_loss_scale != 1.0:
        if not args.fp16:
            print("Warning:  if --fp16 is not used, static_loss_scale will be ignored.")

    if args.optimizer_batch_size < 0:
        batch_size_multiplier = 1
    else:
        tbs = args.world_size * args.batch_size
        if args.optimizer_batch_size % tbs != 0:
            print("Warning: simulated batch size {} is not divisible by actual batch size {}".format(args.optimizer_batch_size, tbs))
        batch_size_multiplier = int(args.optimizer_batch_size/ tbs)
        print("BSM: {}".format(batch_size_multiplier))

    pretrained_weights = None
    if args.pretrained_weights:
        if os.path.isfile(args.pretrained_weights):
            print("=> loading pretrained weights from '{}'".format(args.pretrained_weights))
            pretrained_weights = torch.load(args.pretrained_weights)
        else:
            print("=> no pretrained weights found at '{}'".format(args.resume))

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, map_location = lambda storage, loc: storage.cuda(args.gpu))
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model_state = checkpoint['state_dict']
            optimizer_state = checkpoint['optimizer']
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
            model_state = None
            optimizer_state = None
    else:
        model_state = None
        optimizer_state = None

    if args.resume2:
        if os.path.isfile(args.resume2):
            print("=> loading checkpoint '{}'".format(args.resume2))
            checkpoint2 = torch.load(args.resume2, map_location=lambda storage, loc: storage.cuda(args.gpu))
            model_state2 = checkpoint2['state_dict']
        else:
            model_state2 = None
    else:
        model_state2 = None


    # Create data loaders and optimizers as needed
    if args.dataset == 'cifar10':
        get_train_loader = get_pytorch_train_loader_cifar10
        get_val_loader = get_pytorch_val_loader_cifar10
        get_debug_loader = get_pytorch_debug_loader_cifar10
        QScheme.num_samples = 50000     # NOTE: only needed for use_gradient
    elif args.data_backend == 'pytorch':
        get_train_loader = get_pytorch_train_loader
        get_val_loader = get_pytorch_val_loader
        get_debug_loader = get_pytorch_val_loader
        QScheme.num_samples = 1300000   # NOTE: only needed for use_gradient
    elif args.data_backend == 'dali-gpu':
        get_train_loader = get_dali_train_loader(dali_cpu=False)
        get_val_loader = get_dali_val_loader()
    elif args.data_backend == 'dali-cpu':
        get_train_loader = get_dali_train_loader(dali_cpu=True)
        get_val_loader = get_dali_val_loader()

    loss = nn.CrossEntropyLoss
    if args.mixup > 0.0:
        loss = lambda: NLLMultiLabelSmooth(args.label_smoothing)
    elif args.label_smoothing > 0.0:
        loss = lambda: LabelSmoothing(args.label_smoothing)

    model_and_loss = ModelAndLoss(
            (args.arch, args.model_config),
            args.num_classes,
            loss,
            pretrained_weights=pretrained_weights,
            cuda = True, fp16 = args.fp16)

    train_loader, train_loader_len = get_train_loader(args.data, args.batch_size, args.num_classes, args.mixup > 0.0, workers=args.workers, fp16=args.fp16)
    if args.mixup != 0.0:
        train_loader = MixUpWrapper(args.mixup, args.num_classes, train_loader)

    val_loader, val_loader_len = get_val_loader(args.data, args.batch_size, args.num_classes, False, workers=args.workers, fp16=args.fp16)
    debug_loader, debug_loader_len = get_debug_loader(args.data, args.batch_size, args.num_classes, False, workers=args.workers, fp16=args.fp16)

    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
        logger_backends = [
                    log.JsonBackend(os.path.join(args.workspace, args.raport_file), log_level=1),
                    log.StdOut1LBackend(train_loader_len, val_loader_len, args.epochs, log_level=0),
                ]
        try:
            import wandb
            wandb.init(project="actnn", config=args, name=args.workspace)
            logger_backends.append(log.WandbBackend(wandb))
            print('Logging to wandb...')
        except ImportError:
            print('Wandb not found, logging to stdout and json...')

        logger = log.Logger(args.print_freq, logger_backends)

        for k, v in args.__dict__.items():
            logger.log_run_tag(k, v)
    else:
        logger = None

    optimizer = get_optimizer(list(model_and_loss.model.named_parameters()),
            args.fp16,
            args.lr, args.momentum, args.weight_decay,
            nesterov = args.nesterov,
            bn_weight_decay = args.bn_weight_decay,
            # state=optimizer_state,
            static_loss_scale = args.static_loss_scale,
            dynamic_loss_scale = args.dynamic_loss_scale)


    def new_optimizer():
        return get_optimizer(list(model_and_loss.model.named_parameters()),
            args.fp16,
            args.lr, args.momentum, args.weight_decay,
            nesterov = args.nesterov,
            bn_weight_decay = args.bn_weight_decay,
            # state=optimizer_state,
            static_loss_scale = args.static_loss_scale,
            dynamic_loss_scale = args.dynamic_loss_scale)

    if args.lr_schedule == 'step':
        lr_policy = lr_step_policy(args.lr, [30,60,80], 0.1, args.warmup, logger=logger)
    elif args.lr_schedule == 'cosine':
        lr_policy = lr_cosine_policy(args.lr, args.warmup, args.epochs, logger=logger)
    elif args.lr_schedule == 'linear':
        lr_policy = lr_linear_policy(args.lr, args.warmup, args.epochs, logger=logger)

    if args.amp:
        model_and_loss, optimizer = amp.initialize(
                model_and_loss, optimizer, 
                opt_level="O2", 
                loss_scale="dynamic" if args.dynamic_loss_scale else args.static_loss_scale)

    if args.distributed:
        model_and_loss.distributed(args.local_rank)

    model_and_loss.load_model_state(model_state)

    print('Start epoch {}'.format(args.start_epoch))
    train_loop(
        model_and_loss, optimizer, new_optimizer,
        lr_policy,
        train_loader, val_loader, debug_loader, args.epochs,
        args.fp16, logger, should_backup_checkpoint(args), use_amp=args.amp,
        batch_size_multiplier = batch_size_multiplier,
        start_epoch = args.start_epoch, best_prec1 = best_prec1, prof=args.prof,
        skip_training = args.evaluate, skip_validation = args.training_only,
        save_checkpoints=args.save_checkpoints and not args.evaluate, checkpoint_dir=args.workspace,
        model_state=model_state2)
    exp_duration = time.time() - exp_start_time
    if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
        logger.end()
    print("Experiment ended")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')

    add_parser_arguments(parser)
    args = parser.parse_args()
    cudnn.benchmark = True

    main(args)
