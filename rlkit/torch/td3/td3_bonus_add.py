from collections import OrderedDict

import numpy as np
import torch
import torch.optim as optim
from torch import nn as nn

import rlkit.torch.pytorch_util as ptu
from rlkit.core.eval_util import create_stats_ordered_dict
from rlkit.torch.torch_rl_algorithm import TorchTrainer


class TD3_Bonus_ADD_Trainer(TorchTrainer):
    """
    Twin Delayed Deep Deterministic policy gradients
    """

    def __init__(
            self,
            policy,
            qf1,
            qf2,
            target_qf1,
            target_qf2,
            target_policy,

            bonus_network,
            beta,
            use_bonus_critic,
            use_bonus_policy,

            use_log,

            bonus_norm_param,
            rewards_shift_param,

            device,
            target_policy_noise=0.2,
            target_policy_noise_clip=0.5,

            discount=0.99,
            reward_scale=1.0,

            policy_learning_rate=1e-3,
            qf_learning_rate=1e-3,
            policy_and_target_update_period=2,
            tau=0.005,
            qf_criterion=None,
            optimizer_class=optim.Adam,
    ):
        super().__init__()
        if qf_criterion is None:
            qf_criterion = nn.MSELoss()
        self.qf1 = qf1
        self.qf2 = qf2
        self.policy = policy
        self.target_policy = target_policy
        self.target_qf1 = target_qf1
        self.target_qf2 = target_qf2
        self.target_policy_noise = target_policy_noise
        self.target_policy_noise_clip = target_policy_noise_clip

        self.device = device

        self.bonus_network = bonus_network
        self.beta = beta

        # type of adding bonus to critic or policy
        self.use_bonus_critic = use_bonus_critic
        self.use_bonus_policy = use_bonus_policy

        # use log in the bonus
        # if use_log : log(bonus)
        # else bonus
        self.use_log = use_log

        # normlization
        self.obs_mu, self.obs_std = bonus_norm_param
        self.normalize = self.obs_mu is not None

        if self.normalize:
            print('.......Using normailization in bonus........')
            self.obs_mu = ptu.from_numpy(self.obs_mu).to(device)
            self.obs_std = ptu.from_numpy(self.obs_std).to(device)
            # self.actions_mu = ptu.from_numpy(self.actions_mu).to(device)
            # self.actions_std = ptu.from_numpy(self.actions_std).to(device)

        self.rewards_shift_param = rewards_shift_param


        self.discount = discount
        self.reward_scale = reward_scale

        self.policy_and_target_update_period = policy_and_target_update_period
        self.tau = tau
        self.qf_criterion = qf_criterion

        self.qf1_optimizer = optimizer_class(
            self.qf1.parameters(),
            lr=qf_learning_rate,
        )
        self.qf2_optimizer = optimizer_class(
            self.qf2.parameters(),
            lr=qf_learning_rate,
        )
        self.policy_optimizer = optimizer_class(
            self.policy.parameters(),
            lr=policy_learning_rate,
        )

        self.eval_statistics = OrderedDict()
        self._n_train_steps_total = 0
        self._need_to_update_eval_statistics = True
        self.discrete = False

    def _get_bonus(self, obs, actions):
        if self.normalize:
            obs = (obs - self.obs_mu) / self.obs_std
            # actions = (actions - self.actions_mu) / self.actions_std
        data = torch.cat((obs, actions), dim=1)
        bonus = self.bonus_network(data)

        # use log in the bonus
        # if use_log : log(bonus)
        # else 1 - bonus
        if self.use_log:
            # bonus = torch.log(torch.clamp(bonus, 1e-40, 1))
            bonus = torch.log(bonus)
        else:
            bonus = bonus
        return bonus

    def train_from_torch(self, batch):
        rewards = batch['rewards']
        terminals = batch['terminals']
        obs = batch['observations']
        actions = batch['actions']
        next_obs = batch['next_observations']

        """
        Critic operations.
        """

        next_actions = self.target_policy(next_obs)
        noise = ptu.randn(next_actions.shape) * self.target_policy_noise
        noise = torch.clamp(
            noise,
            -self.target_policy_noise_clip,
            self.target_policy_noise_clip
        )
        noisy_next_actions = next_actions + noise

        target_q1_values = self.target_qf1(next_obs, noisy_next_actions)
        target_q2_values = self.target_qf2(next_obs, noisy_next_actions)
        target_q_values = torch.min(target_q1_values, target_q2_values)

        # use bonus in critic
        if self.use_bonus_critic:
            with torch.no_grad():
                critic_bonus = self._get_bonus(next_obs, noisy_next_actions)
            target_q_values = target_q_values + self.beta * critic_bonus

        q_target = self.reward_scale * rewards + (1. - terminals) * self.discount * target_q_values
        q_target = q_target.detach()

        q1_pred = self.qf1(obs, actions)
        bellman_errors_1 = (q1_pred - q_target) ** 2
        qf1_loss = bellman_errors_1.mean()

        q2_pred = self.qf2(obs, actions)
        bellman_errors_2 = (q2_pred - q_target) ** 2
        qf2_loss = bellman_errors_2.mean()

        """
        Update Networks
        """
        self.qf1_optimizer.zero_grad()
        qf1_loss.backward()
        self.qf1_optimizer.step()

        self.qf2_optimizer.zero_grad()
        qf2_loss.backward()
        self.qf2_optimizer.step()

        policy_actions = policy_loss = None
        if self._n_train_steps_total % self.policy_and_target_update_period == 0:
            policy_actions = self.policy(obs)
            q_output = self.qf1(obs, policy_actions)

            # use bonus in policy
            if self.use_bonus_policy:
                actor_bonus = self._get_bonus(obs, policy_actions)
                q_output = q_output + self.beta * actor_bonus

            policy_loss = - q_output.mean()

            self.policy_optimizer.zero_grad()
            policy_loss.backward()
            self.policy_optimizer.step()

            ptu.soft_update_from_to(self.policy, self.target_policy, self.tau)
            ptu.soft_update_from_to(self.qf1, self.target_qf1, self.tau)
            ptu.soft_update_from_to(self.qf2, self.target_qf2, self.tau)

        if self._need_to_update_eval_statistics:
            self._need_to_update_eval_statistics = False
            if policy_loss is None:
                policy_actions = self.policy(obs)
                q_output = self.qf1(obs, policy_actions)
                policy_loss = - q_output.mean()

            self.eval_statistics['QF1 Loss'] = np.mean(ptu.get_numpy(qf1_loss))
            self.eval_statistics['QF2 Loss'] = np.mean(ptu.get_numpy(qf2_loss))
            self.eval_statistics['Policy Loss'] = np.mean(ptu.get_numpy(
                policy_loss
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q1 Predictions',
                ptu.get_numpy(q1_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q2 Predictions',
                ptu.get_numpy(q2_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q Targets',
                ptu.get_numpy(q_target),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Bellman Errors 1',
                ptu.get_numpy(bellman_errors_1),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Bellman Errors 2',
                ptu.get_numpy(bellman_errors_2),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Policy Action',
                ptu.get_numpy(policy_actions),
            ))
            # bonus
            if self.use_bonus_policy:
                self.eval_statistics.update(create_stats_ordered_dict(
                    'Actor Bonus',
                    ptu.get_numpy(actor_bonus),
                ))
            if self.use_bonus_critic:
                self.eval_statistics.update(create_stats_ordered_dict(
                    'Critic Bonus',
                    ptu.get_numpy(critic_bonus),
                ))
        self._n_train_steps_total += 1

    def get_diagnostics(self):
        return self.eval_statistics

    def end_epoch(self, epoch):
        self._need_to_update_eval_statistics = True

    @property
    def networks(self):
        return [
            self.policy,
            self.qf1,
            self.qf2,
            self.target_policy,
            self.target_qf1,
            self.target_qf2,
        ]

    def get_snapshot(self):
        return dict(
            qf1=self.qf1,
            qf2=self.qf2,
            trained_policy=self.policy,
            target_policy=self.target_policy,
        )
