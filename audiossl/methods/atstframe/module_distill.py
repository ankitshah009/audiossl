from torch import nn
from pytorch_lightning import LightningModule
from audiossl.models.atst.audio_transformer import AST_base, AST_small
from audiossl.methods.atstframe.audio_transformer import FrameAST_base
import argparse
import torch
from audiossl.utils.common import cosine_scheduler_step,get_params_groups
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup
import torch.distributed as dist

from audiossl.modules.head import LinearHead

from audiossl.transforms.common import Normalize,MinMax,RandomCrop,Identity
from torchvision import transforms
import torchaudio
import random
from torch.nn import functional as F
import numpy as np
random.seed(1234)
from audiossl.transforms.byol_a import Mixup, RandomResizeCrop
from audiossl.methods.atstframe.byol import build_mlp
from audiossl.methods.atst.downstream.utils import Metric
from torch.utils.data import WeightedRandomSampler
from audiossl.lightning.datamodules import DistributedSamplerWrapper
import os

from audiossl.methods.atst.downstream.model import PretrainedEncoderPLModule as ClipEncoder
def binary_cross_entropy_with_logits(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Calls BCE with logits and cast the target one_hot (y) encoding to floating point precision."""
    return F.binary_cross_entropy_with_logits(x, y.float())


class DistillATSTTrainTransform:
    def __init__(self,sr=16000,max_len=10,n_mels=64):
        melspec_t = torchaudio.transforms.MelSpectrogram(
            sr, f_min=60, f_max=7800, hop_length=160, win_length=1024, n_fft=1024, n_mels=n_mels)
        to_db = torchaudio.transforms.AmplitudeToDB(stype="power",top_db=80)
        normalize = MinMax(min=-79.6482,max=50.6842)
        self.sr=sr
        self.len=len

        self.mel_feature = transforms.Compose(
                                [melspec_t,
                                to_db,
                                normalize]
                                )

        self.global_transform = transforms.Compose(
                                [RandomCrop(int(sr*max_len),pad=True),
                                self.mel_feature,
                                ]
                                )

    def __call__(self,input):
        output=self.global_transform(input)
        return output,output.shape[-1]
    

class MixupSpecLabelAudioset:
    def __init__(self,dataset,mixup_ratio=0.5,alpha=10,num_classes=527):
        self.dataset = dataset
        self.mixup_ratio = mixup_ratio
        self.alpha = alpha
        self.num_classes=num_classes
    def __call__(self,x,y):

        #rank = dist.get_rank()
        #print("rank {}".format(rank),"id {}".format(get_worker_info()),"seed {}".format(np.random.get_state()[1][0]))

        def convert_label(y):
            """convert label to one hot vector"""
            if isinstance(y,int) or (isinstance(y,torch.Tensor) and (len(y.shape)==0 or y.shape[-1]==1) ):
                if isinstance(y,int):
                    y=torch.nn.functional.one_hot(torch.tensor(y),num_classes=self.num_classes)
                else:
                    y=torch.nn.functional.one_hot(y.to(torch.int64),num_classes=self.num_classes)
            return y
        y = convert_label(y)

        if  np.random.random()<self.mixup_ratio:
            l = np.random.beta(self.alpha,self.alpha,1)
            index = np.random.randint(len(self.dataset))
            (x_,_),y_ = self.dataset[index]
            y_=convert_label(y_)
            if x.shape[-1] == x_.shape[-1]:
                x_mix = x*l + x_*(1-l)
            elif x.shape[-1] > x_.shape[-1]:
                start = np.random.randint(0,x.shape[-1] - x_.shape[-1])
                x_mix = x.clone()

                x_mix[:,:,start:start+x_.shape[-1]] = x[:,:,start:start+x_.shape[-1]]*l + x_*(1-l)
            else:
                start = np.random.randint(0,x_.shape[-1] - x.shape[-1])

                x_mix= x*l + x_[:,:,start:start+x.shape[-1]]*(1-l)
            y_mix = y*l +y_ * (1-l)
        else:
            x_mix=x
            y_mix=y

        return x_mix.to(torch.float),y_mix.to(torch.float)

class DistllATSTTargetTransform:
    def __init__(self,dataset,is_rrc=True,alpha=10,mixup_ratio=0.5):
        self.mixup = MixupSpecLabelAudioset(dataset,mixup_ratio=mixup_ratio,alpha=alpha)
        self.is_rrc = is_rrc
        #self.rrc = RandomResizeCrop((1,1.0),time_scale=(1.0,1.0))
        if self.is_rrc:
            self.rrc = RandomResizeCrop()
    def __call__(self,x,y):
        x,y = self.mixup(x,y)
        if self.is_rrc:
            x = self.rrc(x)
        return x,y



def bool_flag(s):
    """
    Parse boolean arguments from the command line.
    """
    FALSY_STRINGS = {"off", "false", "0"}
    TRUTHY_STRINGS = {"on", "true", "1"}
    if s.lower() in FALSY_STRINGS:
        return False
    elif s.lower() in TRUTHY_STRINGS:
        return True
    else:
        raise argparse.ArgumentTypeError("invalid value for a boolean flag")

class ClipModel(nn.Module):
    def __init__(self,
                 num_labels:int):
        super().__init__()
        self.encoder= AST_base()
        self.head = LinearHead(self.encoder.embed_dim*2, num_labels,use_norm=True, affine=False)
    def forward(self,batch):
        (x, length), y = batch
        x = self.encoder.get_intermediate_layers_chunks(x,
                                                        length,
                                                        1,
                                                        601,
                                                        avgpool=True)
        x = self.head(x)
        return x,y

class Distill(nn.Module):
    def __init__(self,ncls=0,project=False):
        super().__init__()
        self.teacher = ClipModel(527)
        self.student = FrameAST_base(nprompt=ncls)
        self.project=project
        if project:
            self.projector = build_mlp(2,768,4096,768,last_bn=False)
            self.project_linear = LinearHead(768,527,use_norm=True, affine=False)
        self.linear = LinearHead(768,527,use_norm=True, affine=False)
    def forward(self,batch):
        target,_ = self.teacher(batch)
        (x, lengths), y = batch
        pred = self.student.get_intermediate_layers(x,lengths,n=1,scene=True)
        return pred,target,y

def layer_wise_lr_groups(model):

    layer_decay = model.layer_wise_lr
    num_layers = 12
    lr_scales = list(layer_decay ** (num_layers - i) for i in range(num_layers + 1))

    groups = []
    for name,param in model.named_parameters():
        # encoder.mask_embed encoder.pos_embed encoder.patch_embed
        # encoder.blocks.
        # encoder.norm_frame
        # head
        name = name.replace("model.student","encoder") 
        if not param.requires_grad:
            continue

        if name.startswith("encoder.mask_embed") or name.startswith("encoder.pos_embed") or name.startswith("encoder.patch_embed"):
            if model.freeze_embed:
                groups.append({'params': param,
                            'lr_scale' : 0,})
                            #'name':name})
            else:
                groups.append({'params': param,
                            'lr_scale' : lr_scales[0],})
        elif name.startswith("encoder.blocks"):
            index = int(name.split(".")[2])
            groups.append({'params': param,
                        'lr_scale' : lr_scales[index],})
                        #'name':name})
        elif name.startswith("encoder.norm_frame"):
            groups.append({'params': param,
                        'lr_scale' : lr_scales[-2],})
                        #'name':name})
        elif name.startswith("model.linear") or name.startswith("model.project"):
            groups.append({'params': param,
                        'lr_scale' : lr_scales[-1],})
                        #'name':name})
        else:
            print("missed",name)
    print("=====complete==========")
    return groups

class DistillLightningModule(LightningModule):
    def __init__(self,
                 ncls=0,
                 symetric=True,
                 max_steps=100000,
                 warmup_steps=10000,
                 learning_rate:float=5e-4,
                 layer_wise_lr=0.75,
                 freeze_embed=False,
                 lambda_d=1.0,
                 project=False,
                 **kwargs):
        super().__init__()
        self.model = Distill(ncls=ncls,project=project)
        self.learning_rate = learning_rate
        self.layer_wise_lr = layer_wise_lr
        self.freeze_embed = freeze_embed
        self.mylr_scheduler = cosine_scheduler_step(learning_rate,1e-6,max_steps,warmup_steps)
        self.metric = Metric(mode="mAP")
        self.lambda_d = lambda_d
        self.project=project
    def forward(self,x):
        return self.model(x)
    def training_step(self,batch,batch_idx):
        self.schedule()
        (melspecs,lengths),y = batch
        pred,target,_=self.model(batch)

        pred_sup = self.model.linear(pred)
        if self.project:
            pred_ssup = self.model.projector(pred)
            pred_ssup = self.model.project_linear(pred_ssup)
        else:
            pred_ssup = pred_sup
            
        #loss_d =  2 - 2 * F.cosine_similarity(pred, target, dim=-1).mean()
        loss_d =  binary_cross_entropy_with_logits(pred_ssup,target.sigmoid())
        loss_c =  binary_cross_entropy_with_logits(pred_sup,y)
        loss = self.lambda_d*loss_d + (1-self.lambda_d)*loss_c
        self.log("train_loss",loss)
        self.log("loss_c",loss_c,prog_bar=True,logger=True)
        self.log("loss_d",loss_d,prog_bar=True,logger=True)
        return loss
    def schedule(self):
        if self.layer_wise_lr>0:
            for i, param_group in enumerate(self.trainer.optimizers[0].param_groups):
                param_group["lr"] = self.mylr_scheduler[self.global_step] * param_group["lr_scale"]
        else:
            for i, param_group in enumerate(self.trainer.optimizers[0].param_groups):
                param_group["lr"] = self.mylr_scheduler[self.global_step]
        
        self.log("lr",self.mylr_scheduler[self.global_step],prog_bar=True,logger=True)
    def _cal_metric(self,output,target):
        self.metric.update(output.sigmoid(),target)
    def validation_step(self, batch, batch_idx):
        (melspecs,lengths),y = batch
        pred,target,_=self.model(batch)
        #loss_d =  2 - 2 * F.cosine_similarity(pred, target, dim=-1).mean()
        pred_sup = self.model.linear(pred)
        if self.project:
            pred_ssup = self.model.projector(pred)
            pred_ssup = self.model.project_linear(pred_ssup)
        else:
            pred_ssup = pred_sup
        #loss_d =  2 - 2 * F.cosine_similarity(pred, target, dim=-1).mean()
        loss_d =  binary_cross_entropy_with_logits(pred_ssup,target.sigmoid())
        loss_c =  binary_cross_entropy_with_logits(pred_sup,y)
        loss = loss_d + loss_c
        self.log("val_loss",loss,prog_bar=True,logger=True)
        self._cal_metric(pred_sup,y)
        return loss
    def on_validation_epoch_end(self) -> None:
        metric  = self.metric.compute()
        self.metric.clear()
        self.log("val_map",metric,prog_bar=True,logger=True)

    def test_step(self, batch, batch_idx):
        (melspecs,lengths),y = batch
        pred,target,_=self.model(batch)
        pred_sup = self.model.linear(pred)
        self._cal_metric(pred_sup,y)

    def on_test_epoch_end(self) -> None:
        metric  = self.metric.compute()
        self.metric.clear()
        self.log("test_map",metric,prog_bar=True,logger=True)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(layer_wise_lr_groups( self),
                                    self.learning_rate,
                                    momentum=0.9,
                                    weight_decay=0,
                                        )

        return [optimizer]

    

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = parent_parser.add_argument_group("DistillATSTModel")
        parser.add_argument("--symmetric",type=bool_flag,default=True,help="whether to use symemtric loss")
        parser.add_argument("--nprompt",type=int,default=0,help="number of prompts")
        parser.add_argument("--learning_rate", default=0.0005, type=float, help="""Learning rate at the end of
            linear warmup (highest LR used during training). The learning rate is linearly scaled
            with the batch size, and specified here for a reference batch size of 256.""")
        parser.add_argument('--warmup_steps',default=1300,type=int)
        parser.add_argument('--max_steps',default=39010,type=int)
        parser.add_argument('--ncls',default=0,type=int)
        parser.add_argument('--lambda_d',default=1.0,type=float)
        parser.add_argument("--project",type=bool_flag,default=False,help="whether to use symemtric loss")

        return parent_parser
    

from pytorch_lightning import LightningDataModule
from audiossl.datasets import LMDBDataset
from torch.utils import data
class DistillATSTDataModule(LightningDataModule):
    def __init__(self,
                 data_path=None,
                 batch_size_per_gpu=256,
                 num_workers=10,
                 subset=200000,
                 **kwargs,
                 ):
        super().__init__()
        import os
        from torch.utils.data import ConcatDataset
        _dataset_ub=LMDBDataset(data_path,
                                 split="train",
                                 subset=subset,
                                 transform=DistillATSTTrainTransform())
        _dataset_b=LMDBDataset(os.path.join(data_path,"../audioset_b"),
                                 split="train",
                                 subset=subset,
                                 transform=DistillATSTTrainTransform())
        _dataset = ConcatDataset([_dataset_ub,_dataset_b])
        
        dataset_ub=LMDBDataset(data_path,
                                 split="train",
                                 subset=subset,
                                 transform=DistillATSTTrainTransform(),
                                 target_transform=DistllATSTTargetTransform(_dataset))
        dataset_b=LMDBDataset(os.path.join(data_path,"../audioset_b"),
                                 split="train",
                                 subset=subset,
                                 transform=DistillATSTTrainTransform(),
                                 target_transform=DistllATSTTargetTransform(_dataset))
        self.dataset = ConcatDataset([dataset_ub,dataset_b])

        self.val_dataset=LMDBDataset(data_path,
                                 split="valid",
                                 transform=DistillATSTTrainTransform())
        s = torch.load(os.path.join(data_path,"weights_labels.pt"),map_location="cpu")
        weights = s["weights_labels"]
        keys = s["keys"]
        self.dataset.keys = keys
        self.sampler = WeightedRandomSampler(weights, len(weights))
        self.batch_size=batch_size_per_gpu
        self.num_workers=num_workers
        self.save_hyperparameters()
    

    def train_dataloader(self):
        def worker_init_fn(id):
            # seed every worker with different seed
            # so that they don't all get the same samples for MixUp 
            rank = dist.get_rank()
            np.random.seed((id + rank +  np.random.get_state()[1][0])%(2**32))

        return data.DataLoader(self.dataset,
                               batch_size=self.batch_size,
                               num_workers=self.num_workers,
                               shuffle=False,
                               sampler=DistributedSamplerWrapper(self.sampler,range(len(self.sampler))),
                               worker_init_fn=worker_init_fn,
                               drop_last=False,
                               pin_memory=True)
    def val_dataloader(self):
        return data.DataLoader(self.val_dataset,
                               batch_size=self.batch_size,
                               num_workers=self.num_workers,
                               sampler=None,
                               drop_last=False)

    def test_dataloader(self):
        return data.DataLoader(self.val_dataset,
                               batch_size=self.batch_size,
                               num_workers=self.num_workers,
                               sampler=None,
                               drop_last=False)
    @staticmethod
    def add_data_specific_args(parent_parser):
        parser = parent_parser.add_argument_group("DistillATSTData")
        parser.add_argument("--data_path", type=str, default=None, help="data path")
        parser.add_argument('--batch_size_per_gpu', default=256, type=int,
            help='Per-GPU batch-size : number of distinct samples loaded on one GPU.')
        parser.add_argument('--num_workers', default=10, type=int, help='Number of data loading workers per GPU.')
        parser.add_argument('--subset', default=3000000, type=int, help='subset of training data')

        return parent_parser

