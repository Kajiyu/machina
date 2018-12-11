# Copyright 2018 DeepX Inc. All Rights Reserved.
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
# ==============================================================================

import argparse
import json
import os
from pprint import pprint

import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import gym
import pybullet_envs

import machina as mc
from machina.pols import GaussianPol
from machina.algos import ppo_clip, ppo_kl
from machina.vfuncs import DeterministicVfunc
from machina.envs import GymEnv
from machina.data import GAEVectorData
from machina.samplers import ParallelVectorSampler
from machina.misc import logger
from machina.optims import DistributedAdamW
from machina.utils import measure, set_device
from machina.nets.simple_net import PolNetLSTM, VNetLSTM

parser = argparse.ArgumentParser()
parser.add_argument('--log', type=str, default='garbage')
parser.add_argument('--env_name', type=str, default='Pendulum-v0')
parser.add_argument('--record', action='store_true', default=False)
parser.add_argument('--episode', type=int, default=1000000)
parser.add_argument('--seed', type=int, default=256)
parser.add_argument('--max_episodes', type=int, default=1000000)
parser.add_argument('--num_parallel', type=int, default=4)
parser.add_argument('--local_rank', type=int)
parser.add_argument('--world_size', type=int, default=4)

parser.add_argument('--max_samples_per_iter', type=int, default=2048)
parser.add_argument('--epoch_per_iter', type=int, default=4)
parser.add_argument('--batch_size', type=int, default=8)
parser.add_argument('--pol_lr', type=float, default=3e-4)
parser.add_argument('--vf_lr', type=float, default=3e-4)
parser.add_argument('--ent_beta', type=float, default=0.01)
parser.add_argument('--h_size', type=int, default=1024)
parser.add_argument('--cell_size', type=int, default=512)

parser.add_argument('--ppo_type', type=str, choices=['clip', 'kl'], default='clip')

parser.add_argument('--clip_param', type=float, default=0.2)

parser.add_argument('--kl_targ', type=float, default=0.01)
parser.add_argument('--init_kl_beta', type=float, default=1)

parser.add_argument('--gamma', type=float, default=0.995)
parser.add_argument('--lam', type=float, default=1)
args = parser.parse_args()

if not os.path.exists(args.log):
    os.mkdir(args.log)

with open(os.path.join(args.log, 'args.json'), 'w') as f:
    json.dump(vars(args), f)
if args.local_rank == 0:
    pprint(vars(args))

if not os.path.exists(os.path.join(args.log, 'models')):
    os.mkdir(os.path.join(args.log, 'models'))

args.seed = args.seed * (args.local_rank + 23)

dist.init_process_group(backend='nccl', world_size=args.world_size, rank=args.local_rank)

np.random.seed(args.seed)
torch.manual_seed(args.seed)

torch.set_num_threads(1)

device_name = "cuda:{}".format(args.local_rank)
device = torch.device(device_name)
set_device(device)

score_file = os.path.join(args.log, 'progress.csv')
logger.add_tabular_output(score_file)

env = GymEnv(args.env_name, log_dir=os.path.join(args.log, 'movie'), record_video=args.record)
env.env.seed(args.seed)

ob_space = env.observation_space
ac_space = env.action_space

pol_net = PolNetLSTM(ob_space, ac_space, args.h_size, args.cell_size)
pol = GaussianPol(ob_space, ac_space, pol_net)
vf_net = VNetLSTM(ob_space, args.h_size, args.cell_size)
vf = DeterministicVfunc(ob_space, vf_net)

sampler = ParallelVectorSampler(env, pol, args.max_samples_per_iter, args.num_parallel, seed=args.seed)

optim_pol = DistributedAdamW(pol_net.parameters(), args.local_rank, args.world_size, args.pol_lr)
optim_vf = DistributedAdamW(vf_net.parameters(), args.local_rank, args.world_size, args.vf_lr)

total_epi = 0
total_step = 0
max_rew = -1e6
kl_beta = args.init_kl_beta
while args.max_episodes > total_epi:
    with measure('sample'):
        paths = sampler.sample(pol)
    with measure('train'):
        data = GAEVectorData(paths)
        data.preprocess(vf, args.gamma, args.lam, centerize=True)
        if args.ppo_type == 'clip':
            result_dict = ppo_clip.train(data, pol, vf, optim_pol, optim_vf, args.epoch_per_iter, args.batch_size, args.clip_param, args.ent_beta)
        else:
            result_dict = ppo_kl.train(data, pol, vf, kl_beta, args.kl_targ, optim_pol, optim_vf, args.epoch_per_iter, args.batch_size)
            kl_beta = result_dict['new_kl_beta']

    total_epi += data.num_epi
    step = len(paths) * len(paths[0]['rews']) * args.world_size
    total_step += step
    rewards = []
    for path in paths:
        mask = path['dones']
        inds = np.arange(len(mask))
        inds = inds[mask == 1]
        num_epi = len(inds) - 1
        rewards.append(sum(path['rews'][inds[0]:inds[-1]]) / num_epi)
    mean_rew = np.mean(rewards)
    if args.local_rank == 0:
        logger.record_results(args.log, result_dict, score_file,
                              total_epi, step, total_step,
                              rewards,
                              plot_title=args.env_name)

    if args.local_rank == 0:
        if mean_rew > max_rew:
            torch.save(pol.state_dict(), os.path.join(args.log, 'models', 'pol_max.pkl'))
            torch.save(vf.state_dict(), os.path.join(args.log, 'models', 'vf_max.pkl'))
            torch.save(optim_pol.state_dict(), os.path.join(args.log, 'models', 'optim_pol_max.pkl'))
            torch.save(optim_vf.state_dict(), os.path.join(args.log, 'models', 'optim_vf_max.pkl'))
            max_rew = mean_rew

        torch.save(pol.state_dict(), os.path.join(args.log, 'models', 'pol_last.pkl'))
        torch.save(vf.state_dict(), os.path.join(args.log, 'models', 'vf_last.pkl'))
        torch.save(optim_pol.state_dict(), os.path.join(args.log, 'models', 'optim_pol_last.pkl'))
        torch.save(optim_vf.state_dict(), os.path.join(args.log, 'models', 'optim_vf_last.pkl'))
        del data



