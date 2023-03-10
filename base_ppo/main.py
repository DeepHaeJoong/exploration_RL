import os
import time
from typing import Any, Dict, List
from distutils.util import strtobool
import argparse
import yaml
import datetime
from typing import Any, Dict, List

import gymnasium as gym
from gymnasium.spaces import Box, Discrete
from gymnasium.wrappers import TimeLimit 
from gymnasium.wrappers import RecordVideo
# from gymnasium.experimental.wrappers.rendering import RecordVideoV0 as RecordVideo
from gymnasium.envs.registration import register
# import pybullet_envs  # noqa

import numpy as np
import random
import torch
import torch.optim as optim
from torch.distributions import Categorical, Normal
import torch.nn as nn
import torch.nn.functional as F
from minigrid.wrappers import FlatObsWrapper

from torch.utils.tensorboard import SummaryWriter
from tb_logger import TBLogger
import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')


def parse_args():
    parser = argparse.ArgumentParser()

    # ? Experiments information
    parser.add_argument('--exp_name', type=str, default=os.path.basename(__file__).rstrip(".py"),
                        help="the name of this experiment")
    parser.add_argument("--debug", type=bool, default=False)
    parser.add_argument("--load", type=bool, default=False)
    parser.add_argument("--load_ckpt_num", type=int, default=0)
    parser.add_argument("--weight_path", type=str, default=".\weights",
                        help="weight path for saving model")
    parser.add_argument("--save_periods", type=int, default=20)
    parser.add_argument("--results_log_dir", type=str, default=".\logs",
                        help="directory of tensorboard")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggle learning rate annealing for policy and value networks")
    
    # ? Device settings
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)),
                        default=True, nargs="?", const=True,
                        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, cuda will be enabled by default")
    parser.add_argument('--device', default='cuda:0')
    
    # ? Environment settings
    parser.add_argument("--seed", type=int, default=1,
                        help="seed of the experiment")
    parser.add_argument("--render", type=bool, default=False)
    parser.add_argument("--render_mode", type=str, default="human")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")
    parser.add_argument("--is_atari", type=bool, default=False)
    # parser.add_argument("--env_name", type=str, default="CartPole-v1",
    #         help="the id of the environment")
    parser.add_argument("--env_name", type=str, default="LunarLanderContinuous-v2",
            help="the id of the environment")
    # parser.add_argument("--env_name", type=str, default="HalfCheetah-v3")
    # parser.add_argument("--env_name", type=str, default="MiniGrid-DoorKey-16x16-v0") 
    # parser.add_argument("--env_name", type=str, default="Ant")
    parser.add_argument("--total-timesteps", type=int, default=10000000,
                        help="total timesteps of the experiments")
    parser.add_argument('--rollout_steps', default=256)
    parser.add_argument('--max_episode_steps', default=1000)
    parser.add_argument("--num_envs", type=int, default=4,
                        help="the number of parallel game environments")

    # ? Hyperparameter config path
    parser.add_argument('--config_path', type=str,
                        default=".\\base_ppo\\configs\\base_config.yaml")
                        # default="C:\\Users\\KukJinKim\\Desktop\\Exploration\\base_ppo\\configs\\base_config.yaml")
                        
    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.rollout_steps)
    args.now = datetime.datetime.now().strftime('_%m.%d_%H:%M:%S')
    return args


def make_env(args, configs, idx):
    seed = args.seed
    env_id = args.env_name
    capture_video = args.capture_video
    is_atari = args.is_atari
    if is_atari:
        def thunk():
            env = gym.make(env_id)
            env.seed(seed)
            env.action_space.seed(seed)
            env.observation_space.seed(seed)
            env = gym.wrappers.RecordEpisodeStatistics(env)
            if capture_video:
                if idx == 0:
                    env = gym.wrappers.RecordVideo(env, f".\\videos\{run_name}")
            return env
    else:
        def thunk():
            env = gym.make(env_id)
            env = gym.wrappers.RecordEpisodeStatistics(env)
            if capture_video:
                if idx == 0:
                    env = gym.wrappers.RecordVideo(env, f".\\videos\{run_name}")
            if "MiniGrid" in args.env_name:
                print("FlatObsWrapper")
                env = FlatObsWrapper(env)
            else:
                env = gym.wrappers.FlattenObservation(env)
            # env = gym.wrappers.ClipAction(env)
            # env = gym.wrappers.NormalizeObservation(env)
            # env = gym.wrappers.TransformObservation(env, lambda obs: np.clip(obs, -10, 10))
            # env = gym.wrappers.NormalizeReward(configs["gamma"])
            # env = gym.wrappers.TransformReward(env, lambda reward: np.clip(reward, -10, 10))
            return env
    return thunk


def add_state_action_info(env, configs):
    print(env.observation_space)
    print(env.action_space)
    
    if isinstance(env.observation_space, Dict):
        state_dim = env.observation_space.spaces["image"].shape
    else:
        if len(env.observation_space.shape) > 1:   
            state_dim = env.observation_space.shape
        else:
            state_dim = env.observation_space.shape[0]
    
    # ? action_space information
    num_discretes = None
    if isinstance(env.action_space, Box):
        action_dim = env.action_space.shape[0]
        is_continuous = True
    elif isinstance(env.action_space, Discrete):
        action_dim = 1
        num_discretes = env.action_space.n
        is_continuous = False
    configs.update({"state_dim": state_dim,
                    "action_dim": action_dim,
                    "num_discretes": num_discretes,
                    "is_continuous": is_continuous})
    return configs


def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class Agent(nn.Module):
    def __init__(self, args, configs, envs):
        super().__init__()
        self.num_discretes = configs["num_discretes"]
        self.is_continuous = configs["is_continuous"]
        
        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, 1), std=1.0),
        )
        if self.is_continuous:
            self.actor_mean = nn.Sequential(
                layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64)),
                nn.Tanh(),
                layer_init(nn.Linear(64, 64)),
                nn.Tanh(),
                layer_init(nn.Linear(64, np.prod(envs.single_action_space.shape)), std=0.01),
            )
            self.actor_logstd = nn.Parameter(torch.zeros(1, np.prod(envs.single_action_space.shape)))
        
        else:
            self.actor = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, envs.single_action_space.n), std=0.01),
        )

    def get_value(self, x):
        return self.critic(x)

    def get_action_and_value(self, x, action=None):
        if self.is_continuous:
            action_mean = self.actor_mean(x)
            action_logstd = self.actor_logstd.expand_as(action_mean)
            action_std = torch.exp(action_logstd)
            probs = Normal(action_mean, action_std)
            if action is None:
                action = probs.sample()
            return action, probs.log_prob(action).sum(1), probs.entropy().sum(1), self.critic(x)
        else:
            logits = self.actor(x)
            probs = Categorical(logits=logits)
            if action is None:
                action = probs.sample()
            return action, probs.log_prob(action), probs.entropy(), self.critic(x)


if __name__ == "__main__":
    # ? load arguments
    args = parse_args()
    with open(args.config_path, 'rb') as file:
        configs: Dict[str, Any] = yaml.load(file, Loader=yaml.FullLoader)

    args.now = datetime.datetime.now().strftime('_%m.%d_%H_%M_%S')
    run_name = f"{args.exp_name}_{args.env_name}_{args.now}_{args.seed}"
    args.run_name = run_name
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # ? logger
    tb_logger = TBLogger(args, configs)
    
    # ? seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic
    device = torch.device(
        "cuda" if torch.cuda.is_available() and args.cuda else "cpu")


    # ? environments, add state, action informatin in configs
    dummy_env = gym.make(args.env_name)
    configs = add_state_action_info(dummy_env, configs)
    envs = gym.vector.SyncVectorEnv(
        [make_env(args, configs, i) for i in range(args.num_envs)]
    )
    agent = Agent(args, configs, envs).to(device)
    optimizer = optim.Adam(agent.parameters(), lr=float(configs["lr"]), eps=1e-5)

    # ? onpolicy storage for training
    obs = torch.zeros((args.rollout_steps, args.num_envs) + envs.single_observation_space.shape).to(device)
    actions = torch.zeros((args.rollout_steps, args.num_envs) + envs.single_action_space.shape).to(device)
    logprobs = torch.zeros((args.rollout_steps, args.num_envs)).to(device)
    rewards = torch.zeros((args.rollout_steps, args.num_envs)).to(device)
    dones = torch.zeros((args.rollout_steps, args.num_envs)).to(device)
    values = torch.zeros((args.rollout_steps, args.num_envs)).to(device)


    # ? start training, rollout part
    global_step = 0
    start_time = time.time()
    seeds = [i * args.seed for i in range(args.num_envs)]
    # envs.seed(seeds)
    next_obs, info = envs.reset(seed=seeds)
    next_obs = torch.Tensor(next_obs).to(device)
    next_done = torch.zeros(args.num_envs).to(device)
    num_updates = args.total_timesteps // args.batch_size
    for update in range(1, num_updates + 1):
        if global_step % 80000 == 0:
            save_path = os.getcwd()+f'\\base_ppo\\weights\\ppo_{update}.pt'
            torch.save(agent.state_dict(), save_path)
        # Annealing the rate if instructed to do so.
        if args.anneal_lr:
            frac = 1.0 - (update - 1.0) / num_updates
            lrnow = frac * float(configs["lr"])
            optimizer.param_groups[0]["lr"] = lrnow

        for step in range(0, args.rollout_steps):
            global_step += 1 * args.num_envs
            obs[step] = next_obs
            dones[step] = next_done

            # ALGO LOGIC: action logic
            with torch.no_grad():
                action, logprob, _, value = agent.get_action_and_value(next_obs)
                values[step] = value.flatten()
            actions[step] = action
            logprobs[step] = logprob

            # TRY NOT TO MODIFY: execute the game and log data.
            next_obs, reward, truncated, terminated, infos = envs.step(action.cpu().numpy())
            rewards[step] = torch.tensor(reward).to(device).view(-1)
            done = np.logical_or(terminated, truncated)
            next_obs, next_done = torch.Tensor(next_obs).to(device), torch.Tensor(done).to(device)

            if "final_info" not in infos:
                continue

            for info in infos["final_info"]:
                # Skip the envs that are not done
                if info is None:
                    continue
                print(f"global_step={global_step}, episodic_return={info['episode']['r']}")
                tb_logger.add("charts/episodic_return", info["episode"]["r"], global_step)
                tb_logger.add("charts/episodic_length", info["episode"]["l"], global_step)

        # ? calculate GAE
        with torch.no_grad():
            next_value = agent.get_value(next_obs).reshape(1, -1)
            advantages = torch.zeros_like(rewards).to(device)
            lastgaelam = 0
            for t in reversed(range(args.rollout_steps)):
                if t == args.rollout_steps - 1:
                    nextnonterminal = 1.0 - next_done
                    nextvalues = next_value
                else:
                    nextnonterminal = 1.0 - dones[t + 1]
                    nextvalues = values[t + 1]
                delta = rewards[t] + configs["gamma"] * nextvalues * nextnonterminal - values[t]
                advantages[t] = lastgaelam = delta + configs["gamma"] * configs["gae_lambda"] * nextnonterminal * lastgaelam
            returns = advantages + values

        # ? flatten the batch
        b_obs = obs.reshape((-1,) + envs.single_observation_space.shape)
        b_logprobs = logprobs.reshape(-1)
        b_actions = actions.reshape((-1,) + envs.single_action_space.shape)
        b_advantages = advantages.reshape(-1)
        b_returns = returns.reshape(-1)
        b_values = values.reshape(-1)

        # ? Optimizing the policy and value network
        b_inds = np.arange(args.batch_size)
        clipfracs = []
        for epoch in range(configs["k_epochs"]):
            np.random.shuffle(b_inds)
            for start in range(0, args.batch_size, configs["mini_batch_size"]):
                end = start + configs["mini_batch_size"]
                mb_inds = b_inds[start:end]

                _, newlogprob, entropy, newvalue = agent.get_action_and_value(b_obs[mb_inds], b_actions[mb_inds])
                logratio = newlogprob - b_logprobs[mb_inds]
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [((ratio - 1.0).abs() > configs["lr_clip_range"]).float().mean().item()]

                mb_advantages = b_advantages[mb_inds]
                if configs["norm_adv"]:
                    mb_advantages = (mb_advantages - mb_advantages.mean()) / (mb_advantages.std() + 1e-8)

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(ratio, 1 - configs["lr_clip_range"], 1 + configs["lr_clip_range"])
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if configs["clip_vloss"]:
                    v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                    v_clipped = b_values[mb_inds] + torch.clamp(
                        newvalue - b_values[mb_inds],
                        -configs["lr_clip_range"],
                        configs["lr_clip_range"],
                    )
                    v_loss_clipped = (v_clipped - b_returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - b_returns[mb_inds]) ** 2).mean()

                entropy_loss = entropy.mean()
                loss = pg_loss - float(configs["ent_coef"]) * entropy_loss + v_loss * float(configs["vf_coef"])

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(agent.parameters(), configs["max_grad_norm"])
                optimizer.step()

        y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
        var_y = np.var(y_true)
        explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y

        # TRY NOT TO MODIFY: record rewards for plotting purposes
        tb_logger.add("charts/learning_rate", optimizer.param_groups[0]["lr"], global_step)
        tb_logger.add("losses/value_loss", v_loss.item(), global_step)
        tb_logger.add("losses/policy_loss", pg_loss.item(), global_step)
        tb_logger.add("losses/entropy", entropy_loss.item(), global_step)
        tb_logger.add("losses/old_approx_kl", old_approx_kl.item(), global_step)
        tb_logger.add("losses/approx_kl", approx_kl.item(), global_step)
        tb_logger.add("losses/clipfrac", np.mean(clipfracs), global_step)
        tb_logger.add("losses/explained_variance", explained_var, global_step)
        print("SPS:", int(global_step / (time.time() - start_time)))
        tb_logger.add("charts/SPS", int(global_step / (time.time() - start_time)), global_step)

    envs.close()
    tb_logger.close()