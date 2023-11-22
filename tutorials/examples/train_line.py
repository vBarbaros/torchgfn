import random
from typing import ClassVar, Literal, Tuple

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.distributions import Distribution, Normal  # TODO: extend to Beta
from torchtyping import TensorType as TT
from tqdm import trange

from gfn.actions import Actions
from gfn.env import Env
from gfn.gflownet import TBGFlowNet  # TODO: Extend to SubTBGFlowNet
from gfn.modules import GFNModule
from gfn.states import States
from gfn.utils import NeuralNet


class Line(Env):
    """Mixture of Gaussians Line environment."""

    def __init__(
        self,
        mus: list,
        sigmas: list,
        init_value: float,
        n_sd: float = 4.5,
        n_steps_per_trajectory: int = 5,
        device_str: Literal["cpu", "cuda"] = "cpu",
    ):
        assert len(mus) == len(sigmas)
        self.mus = torch.tensor(mus)
        self.sigmas = torch.tensor(sigmas)
        self.n_sd = n_sd
        self.n_steps_per_trajectory = n_steps_per_trajectory
        self.mixture = [Normal(m, s) for m, s in zip(self.mus, self.sigmas)]

        self.init_value = init_value  # Used in s0.
        self.lb = min(self.mus) - self.n_sd * max(self.sigmas)  # Convienience only.
        self.ub = max(self.mus) + self.n_sd * max(self.sigmas)  # Convienience only.
        assert self.lb < self.init_value < self.ub

        s0 = torch.tensor([self.init_value, 0.0], device=torch.device(device_str))
        super().__init__(s0=s0)  # sf is -inf.

    def make_States_class(self) -> type[States]:
        env = self

        class LineStates(States):
            state_shape: ClassVar[Tuple[int, ...]] = (2,)
            s0 = env.s0  # should be [init x value, 0].
            sf = env.sf  # should be [-inf, -inf].

        return LineStates

    def make_Actions_class(self) -> type[Actions]:
        env = self

        class LineActions(Actions):
            action_shape: ClassVar[Tuple[int, ...]] = (1,)  # Does not include counter!
            dummy_action: ClassVar[TT[2]] = torch.tensor(
                [float("inf")], device=env.device
            )
            exit_action: ClassVar[TT[2]] = torch.tensor(
                [-float("inf")], device=env.device
            )

        return LineActions

    def maskless_step(
        self, states: States, actions: Actions
    ) -> TT["batch_shape", 2, torch.float]:
        states.tensor[..., 0] = states.tensor[..., 0] + actions.tensor.squeeze(
            -1
        )  # x position.
        states.tensor[..., 1] = states.tensor[..., 1] + 1  # Step counter.
        return states.tensor

    def maskless_backward_step(
        self, states: States, actions: Actions
    ) -> TT["batch_shape", 2, torch.float]:
        states.tensor[..., 0] = states.tensor[..., 0] - actions.tensor.squeeze(
            -1
        )  # x position.
        states.tensor[..., 1] = states.tensor[..., 1] - 1  # Step counter.
        return states.tensor

    def is_action_valid(
        self, states: States, actions: Actions, backward: bool = False
    ) -> bool:
        # Can't take a backward step at the beginning of a trajectory.
        if torch.any(states[~actions.is_exit].is_initial_state) and backward:
            return False

        return True

    def log_reward(self, final_states: States) -> TT["batch_shape", torch.float]:
        s = final_states.tensor[..., 0]
        # return torch.logsumexp(torch.stack([m.log_prob(s) for m in self.mixture], 0), 0)

        # if s.nelement() == 0:
        #     return torch.zeros(final_states.batch_shape)

        log_rewards = torch.empty((len(self.mixture),) + final_states.batch_shape)
        for i, m in enumerate(self.mixture):
            log_rewards[i] = m.log_prob(s)

        return torch.logsumexp(log_rewards, 0)

    @property
    def log_partition(self) -> float:
        """Log Partition log of the number of gaussians."""
        return torch.tensor(len(self.mus)).log()


def render(env, validation_samples=None):
    """Renders the reward distribution over the 1D env."""
    x = np.linspace(
        min(env.mus) - env.n_sd * max(env.sigmas),
        max(env.mus) + env.n_sd * max(env.sigmas),
        100,
    )

    # Get the rewards from our environment.
    r = env.States(
        torch.tensor(np.stack((x, torch.ones(len(x))), 1))  # Add dummy state counter.
    )
    d = torch.exp(env.log_reward(r))  # Plots the reward, not the log reward.

    fig, ax1 = plt.subplots()

    if not isinstance(validation_samples, type(None)):
        ax2 = ax1.twinx()  # instantiate a second axes that shares the same x-axis.
        ax2.hist(
            validation_samples.tensor[:, 0].cpu().numpy(),
            bins=100,
            density=False,
            alpha=0.5,
            color="red",
        )
        ax2.set_ylabel("Samples", color="red")
        ax2.tick_params(axis="y", labelcolor="red")

    ax1.plot(x, d, color="black")

    # Adds the modes.
    for mu in env.mus:
        ax1.axvline(mu, color="grey", linestyle="--")

    # S0
    ax1.plot([env.init_value], [0], "ro")
    ax1.text(env.init_value + 0.1, 0.01, "$S_0$", rotation=45)

    # Means
    for i, mu in enumerate(env.mus):
        idx = abs(x - mu.numpy()) == min(abs(x - mu.numpy()))
        ax1.plot([x[idx]], [d[idx]], "bo")
        ax1.text(x[idx] + 0.1, d[idx], "Mode {}".format(i + 1), rotation=0)

    ax1.spines[["right", "top"]].set_visible(False)
    ax1.set_ylabel("Reward Value")
    ax1.set_xlabel("X Position")
    ax1.set_title("Line Environment")
    ax1.set_ylim(0, 1)
    plt.show()


class ScaledGaussianWithOptionalExit(Distribution):
    """Extends the Beta distribution by considering the step counter. When sampling,
    the step counter can be used to ensure the `exit_action` [inf, inf] is sampled.
    """

    def __init__(
        self,
        states: TT["n_states", 2],  # Tensor of [x position, step counter].
        mus: TT["n_states", 1],  # Parameter of Gaussian distribution.
        scales: TT["n_states", 1],  # Parameter of Gaussian distribution.
        backward: bool,
        n_steps: int = 5,
    ):
        self.states = states
        self.n_steps = n_steps
        self.dist = Normal(mus, scales)
        self.exit_action = torch.FloatTensor([-float("inf")]).to(states.device)
        self.backward = backward

    def sample(self, sample_shape=()):
        actions = self.dist.sample(sample_shape)

        # For any state which is at the terminal step, assign the exit action.
        if not self.backward:
            idx_at_final_step = self.states[..., 1].tensor == self.n_steps
            exit_mask = torch.where(idx_at_final_step, 1, 0).bool()
            actions[exit_mask] = self.exit_action

        return actions

    def log_prob(self, sampled_actions):
        """TODO"""
        # The default value of logprobs is 0, because these represent the p=1 event
        # of either the terminal forward (Sn->Sf) or backward (S1->S0) transition.
        # We do not explicitly fill these values, but rather set the appropriate
        # logprobs using the `exit_idx` mask.
        logprobs = torch.full_like(sampled_actions, fill_value=0.0)
        actions_to_eval = torch.full_like(sampled_actions, 0)  # Used to remove infs.

        # TODO: Continous Timestamp Environmemt Subclass.
        if self.backward:  # Backward: handle the s1->s0 action (always p=1).
            exit_idx = self.states[..., 1].tensor == 1
        else:  # Forward: handle exit actions: sn->sf.
            exit_idx = torch.all(sampled_actions == -float("inf"), 1)

        actions_to_eval[~exit_idx] = sampled_actions[~exit_idx]
        if sum(~exit_idx) > 0:
            logprobs[~exit_idx] = self.dist.log_prob(actions_to_eval)[~exit_idx]

        return logprobs.squeeze(-1)


class GaussianStepNeuralNet(NeuralNet):
    """A deep neural network for the forward and backward policy."""

    def __init__(
        self,
        hidden_dim: int,
        n_hidden_layers: int,
        policy_std_min: float = 0.1,
        policy_std_max: float = 1,
    ):
        """Instantiates the neural network for the forward policy."""
        assert policy_std_min > 0
        assert policy_std_min < policy_std_max
        self.policy_std_min = policy_std_min
        self.policy_std_max = policy_std_max
        self.input_dim = 2  # [x_pos, counter].
        self.output_dim = 2  # [mus, scales].

        super().__init__(
            input_dim=self.input_dim,
            hidden_dim=hidden_dim,
            n_hidden_layers=n_hidden_layers,
            output_dim=self.output_dim,
            activation_fn="elu",
        )

    def forward(
        self, preprocessed_states: TT["batch_shape", 2, float]
    ) -> TT["batch_shape", "3"]:
        """Calculate the gaussian parameters, applying the bound to sigma."""
        assert preprocessed_states.ndim == 2
        out = super().forward(preprocessed_states)  # [..., 2]: represents mean & std.
        minmax_norm = self.policy_std_max - self.policy_std_min
        out[..., 1] = (
            torch.sigmoid(out[..., 1]) * minmax_norm + self.policy_std_min
        )  # Scales / Variances.

        return out


class StepEstimator(GFNModule):
    """Estimator for PF and PB of the Line environment."""

    def __init__(self, env: Line, module: torch.nn.Module, backward: bool):
        super().__init__(module, is_backward=backward)
        self.backward = backward
        self.n_steps_per_trajectory = env.n_steps_per_trajectory

    def expected_output_dim(self) -> int:
        return 2  # [locs, scales].

    def to_probability_distribution(
        self,
        states: States,
        module_output: TT["batch_shape", "output_dim", float],
        scale_factor=0,  # policy_kwarg.
    ) -> Distribution:
        assert len(states.batch_shape) == 1
        assert module_output.shape == states.batch_shape + (2,)  # [locs, scales].
        locs, scales = torch.split(module_output, [1, 1], dim=-1)

        return ScaledGaussianWithOptionalExit(
            states,
            locs,
            scales + scale_factor,  # Increase this value to induce exploration.
            backward=self.backward,
            n_steps=self.n_steps_per_trajectory,
        )


def fix_seed(seed):
    """Reproducibility."""
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.manual_seed(seed)


def train(
    gflownet,
    env,
    seed=4444,
    n_trajectories=3e6,
    batch_size=128,
    lr_base=1e-3,
    gradient_clip_value=5,
    exploration_var_starting_val=2,
):
    """Trains a GFlowNet on the Line Environment."""
    fix_seed(seed)
    n_iterations = int(n_trajectories // batch_size)

    # TODO: Add in the uniform pb demo?
    # uniform_pb = False
    #
    # if uniform_pb:
    #    pb_module = BoxPBUniform()
    # else:
    #    pb_module = BoxPBNeuralNet(hidden_dim, n_hidden_layers, n_components)

    # 3. Create the optimizer and scheduler.
    optimizer = torch.optim.Adam(gflownet.pf_pb_parameters(), lr=lr_base)
    lr_logZ = lr_base * 100
    optimizer.add_param_group({"params": gflownet.logz_parameters(), "lr": lr_logZ})

    # Training loop.
    states_visited = 0
    tbar = trange(n_iterations, desc="Training iter")
    scale_schedule = np.linspace(exploration_var_starting_val, 0, n_iterations)

    for iteration in tbar:

        # Off Policy Sampling.
        trajectories, estimator_outputs = gflownet.sample_trajectories(
            env,
            n_samples=batch_size,
            sample_off_policy=True,
            scale_factor=scale_schedule[iteration],  # Off policy kwargs.
        )
        training_samples = gflownet.to_training_samples(trajectories)
        optimizer.zero_grad()
        loss = gflownet.loss(env, training_samples, estimator_outputs=estimator_outputs)
        loss.backward()

        # Gradient Clipping.
        for p in gflownet.parameters():
            if p.ndim > 0 and p.grad is not None:  # We do not clip logZ grad.
                p.grad.data.clamp_(
                    -gradient_clip_value, gradient_clip_value
                ).nan_to_num_(0.0)

        optimizer.step()
        states_visited += len(trajectories)

        tbar.set_description(
            "Training iter {}: (states visited={}, loss={:.3f}, estimated logZ={:.3f}, true logZ={:.3f})".format(
                iteration,
                states_visited,
                loss.item(),
                gflownet.logz_parameters()[
                    0
                ].item(),  # Assumes only one estimate of logZ.
                env.log_partition,
            )
        )

    return gflownet


if __name__ == "__main__":

    environment = Line(
        mus=[2, 5],
        sigmas=[0.5, 0.5],
        init_value=0,
        n_sd=4.5,
        n_steps_per_trajectory=5,
    )

    # Hyperparameters.
    hid_dim = 64
    n_hidden_layers = 2
    policy_std_min = 0.1  # Lower bound of sigma that can be predicted by policy.
    policy_std_max = 1  # Upper bound of sigma that can be predicted by policy.
    exploration_var_starting_val = 2  # Used for off-policy training.

    pf_module = GaussianStepNeuralNet(
        hidden_dim=hid_dim,
        n_hidden_layers=n_hidden_layers,
        policy_std_min=policy_std_min,
        policy_std_max=policy_std_max,
    )
    pf = StepEstimator(environment, pf_module, backward=False)

    pb_module = GaussianStepNeuralNet(
        hidden_dim=hid_dim,
        n_hidden_layers=n_hidden_layers,
        policy_std_min=policy_std_min,
        policy_std_max=policy_std_max,
    )
    pb = StepEstimator(environment, pb_module, backward=True)
    gflownet = TBGFlowNet(pf=pf, pb=pb, on_policy=False, init_logZ=0.0)

    gflownet = train(
        gflownet,
        environment,
        lr_base=1e-3,
        n_trajectories=1.28e6,
        batch_size=256,
        exploration_var_starting_val=exploration_var_starting_val,
    )

    validation_samples = gflownet.sample_terminating_states(environment, 10000)
    render(environment, validation_samples=validation_samples)
