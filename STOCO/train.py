import argparse
import logging
import math
import os
import random
import shutil
import time
from collections import OrderedDict

import numpy as np
import torch
import torch.nn.functional as F
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from dataset.data import DATASET_GETTERS
from utils import AverageMeter, accuracy

import ipdb
import gc

logger = logging.getLogger(__name__)
best_acc = 0


def save_checkpoint(state, is_best, checkpoint, filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint,
                                               'model_best.pth.tar'))


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_epochs,
                                    num_training_epochs,
                                    num_steps_per_epoch,
                                    num_cycles=7./16.,
                                    last_epoch=-1):
    def _lr_lambda(current_step):
        num_warmup_steps = num_warmup_epochs * num_steps_per_epoch
        num_training_steps = num_training_epochs * num_steps_per_epoch
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        no_progress = float(current_step - num_warmup_steps) / \
            float(max(1, num_training_steps - num_warmup_steps))
        return max(0., math.cos(math.pi * num_cycles * no_progress))

    return LambdaLR(optimizer, _lr_lambda, last_epoch)


def get_step_schedule_with_warmup(optimizer,
                                  num_warmup_epochs,
                                  num_training_epochs,
                                  num_steps_per_epoch,
                                  decay_epochs=[60, 120, 160, 200],
                                  decay_factor=0.1,
                                  last_epoch=-1):
    def _lr_lambda(current_step):
        num_warmup_steps = num_warmup_epochs * num_steps_per_epoch
        num_training_steps = num_training_epochs * num_steps_per_epoch
        decay_steps = [e * num_steps_per_epoch for e in decay_epochs]
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        exp = current_step > decay_steps[3] and 4 or current_step > decay_steps[2] and 3 or \
        current_step > decay_steps[1] and 2 or current_step > decay_steps[0] and 1 or 0
        return decay_factor ** exp

    return LambdaLR(optimizer, _lr_lambda, last_epoch)


SCHEDULE_GETTERS = {'stl10': get_cosine_schedule_with_warmup,
                    'svhn': get_cosine_schedule_with_warmup,
                    'cifar10': get_cosine_schedule_with_warmup,
                   'cifar100': get_cosine_schedule_with_warmup,
                   'imagenet': get_step_schedule_with_warmup,}


def interleave(x, size):
    s = list(x.shape)
    return x.reshape([-1, size] + s[1:]).transpose(0, 1).reshape([-1] + s[1:])


def de_interleave(x, size):
    s = list(x.shape)
    return x.reshape([size, -1] + s[1:]).transpose(0, 1).reshape([-1] + s[1:])


class whole_model(nn.Module):
    def __init__(self, module1, module2):
        super(whole_model, self).__init__()
        self.module1 = module1
        self.module2 = module2
    
    def forward(self, x):
        x = self.module1(x)
        x = self.module2(x)
        
        return x
    

def main():
    parser = argparse.ArgumentParser(description='PyTorch FixMatch Training')
    parser.add_argument('--gpu-id', default='0', type=int,
                        help='id(s) for CUDA_VISIBLE_DEVICES')
    parser.add_argument('--num-workers', default=4, type=int, 
                        help='number of workers')
    parser.add_argument('--dataset', default='cifar10', type=str,
                        choices=['stl10', 'svhn', 'cifar10', 'cifar100', 'imagenet'],
                        help='dataset name')
    parser.add_argument('--root', default='', type=str,
                        help='dataset folder')
    parser.add_argument('--num-labeled', default=4000, type=int, 
                        help='number of labeled data')
    parser.add_argument("--expand-labels", action="store_true",
                        help="expand labels to fit eval steps")
    parser.add_argument('--arch', default='wideresnet', type=str,
                        choices=['wideresnet', 'resnet50'],
                        help='dataset name')
    parser.add_argument('--total-steps', default=2**20, type=int,
                        help='number of total steps to run')
    parser.add_argument('--eval-step', default=1024, type=int,
                        help='number of eval steps to run')
    parser.add_argument('--start-epoch', default=0, type=int,
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('--batch-size', default=64, type=int,
                        help='train batchsize')
    parser.add_argument('--lr', '--learning-rate', default=0.03, type=float,
                        help='initial learning rate')
    parser.add_argument('--warmup', default=0, type=int,
                        help='warmup epochs (unlabeled data based)')
    parser.add_argument('--wdecay', default=5e-4, type=float,
                        help='weight decay')
    parser.add_argument('--nesterov', action='store_true', default=True,
                        help='use nesterov momentum')
    parser.add_argument('--use-ema', action='store_true', default=True,
                        help='use EMA model')
    parser.add_argument('--ema-decay', default=0.999, type=float,
                        help='EMA decay rate')
    parser.add_argument('--mu', default=7, type=int,
                        help='coefficient of unlabeled batch size')
    parser.add_argument('--lambda-u', default=1, type=float,
                        help='coefficient of unlabeled loss')
    parser.add_argument('--T', default=1, type=float,
                        help='pseudo label temperature')
    parser.add_argument('--rm_aug_s', action='store_true', 
                        help='whether to backward the loss on weakly augmented unlabeled samples instead of that on strongly augmented ones')
    parser.add_argument('--pseudo_label_method', default='depict', type=str,
                        choices=['depict', 'fixmatch'],
                        help='method of generating pseudo label')
    parser.add_argument('--classifier_type', default='stochastic', type=str,
                        choices=['stochastic', 'vanilla'],
                        help='classifier type')
    parser.add_argument('--num_classifiers', default=1, type=int,
                        help='number of sampled classifiers')
    parser.add_argument('--confidence-threshold', default=0.95, type=float, 
                        help='pseudo label confidence threshold')
    parser.add_argument('--out', default='results/', type=str, 
                        help='directory to output results')
    parser.add_argument('--resume', default='', type=str,
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('--seed', default=None, type=int,
                        help="random seed")
    parser.add_argument("--amp", action="store_true",
                        help="use 16-bit (mixed) precision through NVIDIA apex AMP")
    parser.add_argument("--opt_level", default="O1", type=str, 
                        help="apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                        "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", default=-1, type=int, 
                        help="For distributed training: local_rank")
    parser.add_argument('--no-progress', action='store_true',
                        help="don't use progress bar")

    args = parser.parse_args()
    global best_acc

    def create_model(args):
        if args.arch == 'wideresnet':
            import models.wideresnet as models
            model = models.build_wideresnet(depth=args.model_depth,
                                            widen_factor=args.model_width,
                                            dropout=0,
                                            num_classes=args.num_classes,
                                            dataset=args.dataset,
                                            classifier_type=args.classifier_type)
        elif args.arch == 'resnet50':
            import models.preact_resnet as models
            model = models.build_preact_resnet(args)
            
        logger.info("Total params: {:.2f}M".format(
            (sum(p.numel() for p in model['G'].parameters()) + sum(p.numel() for p in model['F'].parameters())) /1e6))
        
        return model

    if args.local_rank == -1:
        device = torch.device('cuda', args.gpu_id)
        args.world_size = 1
        args.n_gpu = torch.cuda.device_count()
    else:
        print('world_size:', os.environ['WORLD_SIZE'])
        args.world_size = int(os.environ['WORLD_SIZE'])
        print('rank:', os.environ["RANK"])
        args.rank = int(os.environ["RANK"])
        torch.cuda.set_device(args.local_rank)
        device = torch.device('cuda', args.local_rank)
        torch.distributed.init_process_group(backend='nccl')#, init_method='tcp://222.201.134.186:23456', rank=args.rank, world_size=args.world_size)
        #args.world_size = torch.distributed.get_world_size()
        args.n_gpu = 1

    args.device = device
    
    if args.dataset == 'cifar10' or args.dataset == 'svhn':
        args.num_classes = 10
        args.model_depth = 28
        args.model_width = 2

    elif args.dataset == 'cifar100':
        args.num_classes = 100
        args.model_depth = 28
        args.model_width = 8
    
    elif args.dataset == 'imagenet':
        args.num_classes = 1000
        args.num_labeled = int(0.1 * 1281167 / args.num_classes) * args.num_classes
        args.mu = 5
        args.eval_step = 1281167 // (args.batch_size * args.mu * args.world_size)
        args.total_steps = 300 * args.eval_step
        args.warmup = 5
        args.lambda_u = 10
        
    elif args.dataset == 'stl10':
        args.num_classes = 10
        args.model_depth = 37
        args.model_width = 2

    if args.seed is not None:
        set_seed(args)
    
    args.out += args.dataset + '@' + str(args.num_labeled)
    args.out = os.path.join(args.out, 'pseudo_label_method-' + args.pseudo_label_method + '_num_f-' + str(args.num_classifiers) + '_conf_thred-' + str(args.confidence_threshold) + '_lam_u-' + str(args.lambda_u) + '_mu-' + str(args.mu) + '_lr-' + str(args.lr) + '_arch-' + args.arch + '_classifier_type-' + args.classifier_type + '_seed-' + str(args.seed))
        
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()

    if args.local_rank in [-1, 0]:
        os.makedirs(args.out, exist_ok=True)
        args.writer = SummaryWriter(args.out)
    
    if args.local_rank == 0:
        torch.distributed.barrier()
            
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
        filename=os.path.join(args.out, 'log.txt'), 
        filemode='a',
        )

    logger.warning(
        f"Process rank: {args.local_rank}, "
        f"device: {args.device}, "
        f"n_gpu: {args.n_gpu}, "
        f"distributed training: {bool(args.local_rank != -1)}, "
        f"16-bits training: {args.amp}",)

    logger.info(dict(args._get_kwargs()))                        

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()
        
    labeled_dataset, unlabeled_dataset, test_dataset = DATASET_GETTERS[args.dataset](args)
    
    if args.local_rank == 0:
        torch.distributed.barrier()

    train_sampler = RandomSampler if args.local_rank == -1 else DistributedSampler

    labeled_trainloader = DataLoader(
        labeled_dataset,
        sampler=train_sampler(labeled_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        drop_last=True)

    unlabeled_trainloader = DataLoader(
        unlabeled_dataset,
        sampler=train_sampler(unlabeled_dataset),
        batch_size=args.batch_size*args.mu,
        num_workers=args.num_workers,
        drop_last=True)

    test_loader = DataLoader(
        test_dataset,
        sampler=SequentialSampler(test_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers)

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()

    model = create_model(args)

    if args.local_rank == 0:
        torch.distributed.barrier()
    
    if torch.cuda.is_available() and args.n_gpu > 1 and args.local_rank == -1:
        model = {k: torch.nn.DataParallel(v) for k,v in model.items()}

    model['G'].to(args.device)
    model['F'].to(args.device)

    no_decay = ['bias', 'bn']
    grouped_parameters = [
        {'params': [p for n, p in model['G'].named_parameters() if not any(
            nd in n for nd in no_decay)], 'weight_decay': args.wdecay},
        {'params': [p for n, p in model['G'].named_parameters() if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0},            
        {'params': [p for n, p in model['F'].named_parameters() if not any(
            nd in n for nd in no_decay)], 'weight_decay': args.wdecay},
        {'params': [p for n, p in model['F'].named_parameters() if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = optim.SGD(grouped_parameters, lr=args.lr,
                          momentum=0.9, nesterov=args.nesterov)

    args.epochs = math.ceil(args.total_steps / args.eval_step)
    scheduler = SCHEDULE_GETTERS[args.dataset](optimizer, args.warmup, args.epochs, args.eval_step)

    ema_model = {}
    if args.use_ema:
        from models.ema import ModelEMA
        ema_model['G'] = ModelEMA(args, model['G'], args.ema_decay)
        ema_model['F'] = ModelEMA(args, model['F'], args.ema_decay)

    if args.amp:
        from apex import amp
        model_whole = whole_model(model['G'], model['F'])
        model_whole, optimizer = amp.initialize(
            model_whole, optimizer, opt_level=args.opt_level)
        model['G'] = model_whole.module1
        model['F'] = model_whole.module2

    if args.local_rank != -1:
        #model['G'] = nn.SyncBatchNorm.convert_sync_batchnorm(model['G']) # prevent multiple gpus from reducing the batch size
        #model['F'] = nn.SyncBatchNorm.convert_sync_batchnorm(model['F'])
        model['G'] = torch.nn.parallel.DistributedDataParallel(
            model['G'], device_ids=[args.local_rank],
            output_device=args.local_rank, find_unused_parameters=True)
        model['F'] = torch.nn.parallel.DistributedDataParallel(
            model['F'], device_ids=[args.local_rank],
            output_device=args.local_rank, find_unused_parameters=True)
    
    args.start_epoch = 0

    if args.resume:
        logger.info("==> Resuming from checkpoint..")
        assert os.path.isfile(
            args.resume), "Error: no checkpoint directory found!"
        args.out = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_acc = checkpoint['best_acc']
        args.start_epoch = checkpoint['epoch']
        model['G'].load_state_dict(checkpoint['state_dict_G'])
        model['F'].load_state_dict(checkpoint['state_dict_F'])
        if args.use_ema:
            ema_model['G'].ema.load_state_dict(checkpoint['ema_state_dict_G'])
            ema_model['F'].ema.load_state_dict(checkpoint['ema_state_dict_F'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        scheduler.load_state_dict(checkpoint['scheduler'])
        del checkpoint
        gc.collect()
        torch.cuda.empty_cache()
        if args.local_rank != -1:
            torch.distributed.barrier()
        
    logger.info("***** Running training *****")
    logger.info(f"  Task = {args.dataset}@{args.num_labeled}")
    logger.info(f"  Num Epochs = {args.epochs}")
    logger.info(f"  Batch size per GPU = {args.batch_size}")
    logger.info(
        f"  Total train batch size = {args.batch_size*args.world_size}")
    logger.info(f"  Total optimization steps = {args.total_steps}")

    model['G'].zero_grad()
    model['F'].zero_grad()
    print(model['G'])
    print(model['F'])
    train(args, labeled_trainloader, unlabeled_trainloader, test_loader,
          model, optimizer, ema_model, scheduler)
    

def train(args, labeled_trainloader, unlabeled_trainloader, test_loader,
          model, optimizer, ema_model, scheduler):
    if args.amp:
        from apex import amp
    global best_acc
    test_accs = []
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_u = AverageMeter()
    mask_probs = AverageMeter()
    noise_rates = AverageMeter()
    mislabeled_nums = AverageMeter()
    noise_rate_estms = AverageMeter()
    mislabeled_num_estms = AverageMeter()
    end = time.time()

    labeled_iter = iter(labeled_trainloader)
    unlabeled_iter = iter(unlabeled_trainloader)
    
    for epoch in range(args.start_epoch, args.epochs):
        model['G'].train()
        model['F'].train()
    
        if not args.no_progress:
            p_bar = tqdm(range(args.eval_step),
                         disable=args.local_rank not in [-1, 0])
        for batch_idx in range(args.eval_step):
            try:
                inputs_x, targets_x = labeled_iter.next()
            except:
                labeled_iter = iter(labeled_trainloader)
                inputs_x, targets_x = labeled_iter.next()

            try:
                (inputs_u_w, inputs_u_s), targets_u_gt = unlabeled_iter.next()
            except:
                unlabeled_iter = iter(unlabeled_trainloader)
                (inputs_u_w, inputs_u_s), targets_u_gt = unlabeled_iter.next()
            targets_u_gt = targets_u_gt.to(args.device) # only for visualization

            data_time.update(time.time() - end)
            batch_size = inputs_x.shape[0]
            inputs = interleave(
                torch.cat((inputs_x, inputs_u_w, inputs_u_s)), 2*args.mu+1).to(args.device)
            targets_x = targets_x.to(args.device)
            features = model['G'](inputs)
            logits = model['F'](features)
            logits = de_interleave(logits, 2*args.mu+1)
            logits_x = logits[:batch_size]
            logits_u_w, logits_u_s = logits[batch_size:].chunk(2)
            del logits
            gc.collect()
            
            pseudo_label1 = torch.softmax(logits_u_w.detach()/args.T, dim=-1)
            pseudo_label_mul = pseudo_label1.clone()
            pseudo_label_sum = pseudo_label1.clone()
            features_u_w = de_interleave(features, 2*args.mu+1)[batch_size:batch_size+inputs_u_w.size(0)]
            for _ in range(args.num_classifiers - 1):
                prob = torch.softmax(model['F'](features_u_w).detach()/args.T, dim=-1)
                pseudo_label_mul *= prob
                pseudo_label_sum += prob
            pseudo_label_sum /= args.num_classifiers
            pseudo_label = pseudo_label_mul
            max_probs, targets_u = torch.max(pseudo_label, dim=-1)
            mask = max_probs.ge(args.confidence_threshold).float()
            
            Lx = F.cross_entropy(logits_x, targets_x, reduction='mean')
            
            logits_u = logits_u_w if args.rm_aug_s else logits_u_s
            if args.pseudo_label_method == 'depict':
                if args.local_rank != -1:
                    #print(pseudo_label_sum.size()) # 112*10
                    tensor_list = [pseudo_label_sum.clone()] * args.world_size
                    torch.distributed.all_gather(tensor_list, pseudo_label_sum)
                    tensor_list = torch.cat(tensor_list, dim=0)
                    #print(tensor_list.size()) # 448*10
                    #print(tensor_list) # the same for 4 processes
                else:
                    tensor_list = pseudo_label_sum
                targets_u_aux = pseudo_label_sum / tensor_list.sum(0, keepdim=True).pow(0.5) # depict clustering, the denominator is affected by distributed training (batch size 16 for STL-10)
                targets_u_aux /= targets_u_aux.sum(1, keepdim=True)
                Lu = - ((targets_u_aux * F.log_softmax(logits_u, dim=-1)).sum(1) * mask).mean()
            elif args.pseudo_label_method == 'fixmatch':
                Lu = (F.cross_entropy(logits_u, targets_u, reduction='none') * mask).mean()

            loss = Lx + args.lambda_u * Lu
            if args.amp:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()
            
            losses.update(loss.item())
            losses_x.update(Lx.item())
            losses_u.update(Lu.item())
            optimizer.step()
            scheduler.step()
            if args.use_ema:
                ema_model['G'].update(model['G'])
                ema_model['F'].update(model['F'])
            model['G'].zero_grad()
            model['F'].zero_grad()

            batch_time.update(time.time() - end)
            end = time.time()
            mask_probs.update(mask.mean().item())
            if mask.sum() != 0:
                noise_rates.update((targets_u != targets_u_gt)[mask == 1].float().mean().item(), mask.sum())
                mislabeled_nums.update((targets_u != targets_u_gt)[mask == 1].float().sum().item())
                noise_rate_estms.update((logits_x.max(-1)[1] != targets_x).float().mean().item())
                mislabeled_num_estms.update(((logits_x.max(-1)[1] != targets_x).float().mean() * mask.sum()).item())
            if not args.no_progress:
                p_bar.set_description("Train Epoch: {epoch}/{epochs:4}. Iter: {batch:4}/{iter:4}. LR: {lr:.4f}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. Loss_x: {loss_x:.4f}. Loss_u: {loss_u:.4f}. Mask: {mask:.2f}.".format(
                    epoch=epoch + 1,
                    epochs=args.epochs,
                    batch=batch_idx + 1,
                    iter=args.eval_step,
                    lr=scheduler.get_last_lr()[0],
                    data=data_time.avg,
                    bt=batch_time.avg,
                    loss=losses.avg,
                    loss_x=losses_x.avg,
                    loss_u=losses_u.avg,
                    mask=mask_probs.avg,))
                p_bar.update()

        if not args.no_progress:
            p_bar.close()
        
        if args.use_ema:
            test_model = {}
            test_model['G'] = ema_model['G'].ema
            test_model['F'] = ema_model['F'].ema
        else:
            test_model = model

        if args.local_rank in [-1, 0]:
            test_loss, test_acc = test(args, test_loader, test_model, epoch)

            args.writer.add_scalar('train/1.train_loss', losses.avg, epoch)
            args.writer.add_scalar('train/2.train_loss_x', losses_x.avg, epoch)
            args.writer.add_scalar('train/3.train_loss_u', losses_u.avg, epoch)
            args.writer.add_scalar('train/4.mask', mask_probs.avg, epoch)
            args.writer.add_scalar('train/5.noise_rate', noise_rates.avg, epoch)
            args.writer.add_scalar('train/6.mislabeled_num', mislabeled_nums.avg, epoch)
            args.writer.add_scalar('train/7.noise_rate_estm', noise_rate_estms.avg, epoch)
            args.writer.add_scalar('train/8.mislabeled_num_estm', mislabeled_num_estms.avg, epoch)
            args.writer.add_scalar('test/1.test_acc', test_acc, epoch)
            args.writer.add_scalar('test/2.test_loss', test_loss, epoch)

            is_best = test_acc > best_acc
            best_acc = max(test_acc, best_acc)

            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict_G': model['G'].state_dict(),
                'state_dict_F': model['F'].state_dict(),
                'ema_state_dict_G': ema_model['G'].ema.state_dict() if args.use_ema else None,
                'ema_state_dict_F': ema_model['F'].ema.state_dict() if args.use_ema else None,
                'acc': test_acc,
                'best_acc': best_acc,
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict(),
            }, is_best, args.out)

            test_accs.append(test_acc)
            logger.info('Best top-1 acc: {:.2f}'.format(best_acc))
            logger.info('Mean top-1 acc: {:.2f}\n'.format(
                np.mean(test_accs[-20:])))

    if args.local_rank in [-1, 0]:
        args.writer.close()


def test(args, test_loader, model, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    if not args.no_progress:
        test_loader = tqdm(test_loader,
                           disable=args.local_rank not in [-1, 0])

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(test_loader):
            data_time.update(time.time() - end)
            model['G'].eval()
            model['F'].eval()

            inputs = inputs.to(args.device)
            targets = targets.to(args.device)
            outputs = model['F'](model['G'](inputs), mode='test')
            loss = F.cross_entropy(outputs, targets)

            prec1, prec5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.shape[0])
            top1.update(prec1.item(), inputs.shape[0])
            top5.update(prec5.item(), inputs.shape[0])
            batch_time.update(time.time() - end)
            end = time.time()
            if not args.no_progress:
                test_loader.set_description("Test Iter: {batch:4}/{iter:4}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. top1: {top1:.2f}. top5: {top5:.2f}. ".format(
                    batch=batch_idx + 1,
                    iter=len(test_loader),
                    data=data_time.avg,
                    bt=batch_time.avg,
                    loss=losses.avg,
                    top1=top1.avg,
                    top5=top5.avg,
                ))
        if not args.no_progress:
            test_loader.close()

    logger.info("epoch: {}".format(epoch))
    logger.info("top-1 acc: {:.2f}".format(top1.avg))
    logger.info("top-5 acc: {:.2f}".format(top5.avg))
        
    return losses.avg, top1.avg


if __name__ == '__main__':
    main()
