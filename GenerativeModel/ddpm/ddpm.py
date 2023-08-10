import math
import numpy as np
from inspect import isfunction
from functools import partial

import matplotlib.pyplot as plt
from tqdm.auto import tqdm
# from einops import rearrange, reduce
# from einops.layers.torch import Rearrange

from PIL import Image
import requests

import torch
from torch import nn, einsum
import torch.nn.functional as F

import matplotlib.image as img
import matplotlib.pyplot as plt

from torchvision.transforms import Compose, ToTensor, Lambda, ToPILImage, CenterCrop, Resize


# Network helper


# Defining the forward diffusion process
class ForwardBetaSchedule():
    @staticmethod
    def cosine_beta_schedule(timesteps, s=0.008):
        """
        cosine schedule as proposed in https://arxiv.org/abs/2102.09672
        - cosine 스케줄링은 저해상도에서 유용. 고해상도 이미지에서는 linear로 충분
        """
        
        steps = timesteps + 1
        x = torch.linspace(0, timesteps, steps)
        alphas_cumprod = torch.cos(((x / timesteps) + s) / (1 + s) * torch.pi * 0.5) ** 2
        alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
        betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
        return torch.clip(betas, 0.0001, 0.9999)
    
    @staticmethod
    def linear_beta_schedule(timesteps):
        beta_start = 0.0001
        beta_end = 0.02
        return torch.linspace(beta_start, beta_end, timesteps)
    
    @staticmethod
    def quadratic_beta_schedule(timesteps):
        beta_start = 0.0001
        beta_end = 0.02
        return torch.linspace(beta_start**0.5, beta_end**0.5, timesteps) ** 2
    
    @staticmethod
    def sigmoid_beta_schedule(timesteps):
        beta_start = 0.0001
        beta_end = 0.02
        betas = torch.linspace(-6, 6, timesteps)
        return torch.sigmoid(betas) * (beta_end - beta_start) + beta_start



def load_image():
    url = 'http://images.cocodataset.org/val2017/000000039769.jpg'
    image = Image.open(requests.get(url, stream=True).raw) # PIL image of shape HWC
    plt.imshow(image)
    plt.show()
    return image


def extract(alphas, target_t, x_shape):
    """
    처음부터 끝까지 구해놓은 alphas의 텐서와 목표하는 timestep인 텐서 target_t를 받아
    target_t를 index로 씀으로 alphas에서 값을 하나 추출한다.
    out = alphas.gather(-1, target_t.cpu())에서 
    alphas의 shape가 300이면 300개 중 target(여기선 40)의 인덱스의 위치의 값을 가져온다.
    """
    batch_size = target_t.shape[0]
    out = alphas.gather(-1, target_t.cpu()) # dim=-1은 차원에서 마지막 차원을 뜻한다.(무조건 열일듯), index: take로 취할 단일 텐서

    return out.reshape(batch_size, *((1,) * (len(x_shape) - 1))).to(target_t.device) # (1, 1, 1, 1)shpae으로 값 return. 


class Diffusion(nn.Module):
    def __init__(
            self, 
            total_timesteps = 300
    ):
        """
        아래 변수 모두 timestep만큼의 size를 가지고 있다. 학습이랑 샘플링 때 필요한 t에 맞게 index를 가져가면 된다.

        # Forward process
        alphas: 
            ddpm (eq.4)위에 alpha의 정의가 있다. 1 - beta가 알파이다.

        alphas_cumprod: 
            ddpm (eq.4)위에 alpha_bar의 정의가 있다. 1~t까지의 누적곱을 뜻한다.

        alphas_cumprod_prev: 
            샘플링 과정(eq.6)에서 뮤 틸드를 구할 때 필요. 
            알파 바의 t-1이 필요하기에 맨 마지막을 제거하고 맨 처음에 알파가 하나도 없었다는 뜻의 1을 집어넣는다.

        # Training
        sqrt_alphas_cumprod, sqrt_one_minus_alphas_cumprod:
                학습 과정에서 필요. DDPM의 Algorithm 1 Training 참고.

        # Sampling
        sqrt_recip_alphas = torch.sqrt(1.0 / self.alphas):
            루트 알파 바의 역수가 샘플링 과정에서 필요하다. 
        
        # ??
        posterior_variance:
            사후 확률 q(x_{t-1} | x_t, x_0)(eq.6.) 계산에서 분산에 사용되는 값. beta tilde로 표현된다.
            KL divergence를 사용하여 바로 pθ(xt−1|xt)와 forward process의 posterior와 비교하는데, 
            이 때 X_0를 조건으로 하면 forward process의 posterior를 추적할 수 있다.
            즉 구하고자 하는 t와, 처음 시작지점 x_0를 조건으로 주면 x_{t-1}의 노이즈 상태를 얻을 수 있다.
        """
        # beta schedule 정의
        self.betas = ForwardBetaSchedule.linear_beta_schedule(timesteps=total_timesteps)

        self.alphas = 1. - self.betas
        self.alphas_cumprod = torch.cumprod(self.alphas, axis=0)
        self.alphas_cumprod_prev = F.pad(self.alphas_cumprod[:-1], (1, 0), value=1.0) 

        self.sqrt_alphas_cumprod = torch.sqrt(self.alphas_cumprod) 
        self.sqrt_one_minus_alphas_cumprod = torch.sqrt(1. - self.alphas_cumprod) 

        self.sqrt_recip_alphas = torch.sqrt(1.0 / self.alphas)

        self.posterior_variance = self.betas * (1. - self.alphas_cumprod_prev) / (1. - self.alphas_cumprod)

    
    # forward process. 목표하는 타입스텝까지의 tensor들을 extract해서 텐서를 가져온다.
    def q_sample(self, x_start:torch.Tensor, target_t:torch.Tensor, noise=None):
        """
        sqrt_alphas_cumprod_t * x_start + sqrt_one_minus_alphas_cumprod_t * noise의 이해
        식 4를 참조하면 평균에서 \sqrt{\hat{\alpha}} * x_0 (여기선 x_start)를 확인할 수 있으며
        표준편차에서 (1 - \hat{\alpha_t}) * I 를 볼 수 있다. 해당 수식과 상당히 유사. (I가 noise로 추정. )
        즉, 평균에서 표준편차만큼 계속 더해주면 노이즈라는건가?

        기존 노이즈 * 시작 이미지 + 추가 노이즈
        """
        if noise is None:
            noise = torch.randn_like(x_start) # torch.randn(): 정규분포를 이용하여 생성

        # 학습할 때 아래 값들이 필요하다.
        sqrt_alphas_cumprod_t = extract(self.sqrt_alphas_cumprod, target_t, x_start.shape)
        sqrt_one_minus_alphas_cumprod_t = extract(self.sqrt_one_minus_alphas_cumprod, target_t, x_start.shape)

        return sqrt_alphas_cumprod_t * x_start + sqrt_one_minus_alphas_cumprod_t * noise
    

    def get_noisy_image(self, x_start, target_t):
        x_noisy = self.q_sample(x_start, target_t)
        
        # 역변환을 정의하여, [-1, 1]의 값을 포함하는 PyTorch 텐서를 받아 이를 다시 PIL 이미지로 변환
        reverse_transform = Compose([
            Lambda(lambda t: (t + 1) / 2),
            Lambda(lambda t: t.permute(1, 2, 0)), # CHW to HWC
            Lambda(lambda t: t * 255.),
            Lambda(lambda t: t.numpy().astype(np.uint8)),
            ToPILImage(), # tensor or ndarray to PIL image
        ])

        noisy_image = reverse_transform(x_noisy.squeeze())

        return noisy_image

    
    def test_forward_process(self):
        image = load_image()
        
        image_size = 128
        transform = Compose([
            Resize(image_size),
            CenterCrop(image_size),
            ToTensor(), # C,H,W shape의 tensor로 변환하며 255로 나누어 0~1 사이의 값으로 정규화
            Lambda(lambda t: (t * 2) - 1), # [-1, 1] 사이로 값 정규화
        ])

        # 이미지 전처리 후 '0'dim 인덱스에 '1'인 차원을 추가한다. ex) [3,128,128] -> [1,3,128,128]        
        # 원본 이미지
        x_start = transform(image).unsqueeze(0) 

        # take time step
        t = torch.tensor([40])

        # get noisy_image
        noisy_image = self.get_noisy_image(x_start, t)
        
        print(type(noisy_image))
        plt.imshow(noisy_image)
        plt.show()

        


# 정리된 main
if __name__ == '__main__':
    model = Diffusion(total_timesteps=300)
    model.test_forward_process()

