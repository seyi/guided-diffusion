import numpy as np
import tensorflow.compat.v1 as tf

from . import nn
from . import utils


def normal_kl(mean1, logvar1, mean2, logvar2):
  """
  KL divergence between normal distributions parameterized by mean and log-variance.
  """
  return 0.5 * (-1.0 + logvar2 - logvar1 + tf.exp(logvar1 - logvar2)
                + tf.squared_difference(mean1, mean2) * tf.exp(-logvar2))


def _warmup_beta(beta_start, beta_end, num_diffusion_timesteps, warmup_frac):
  betas = beta_end * np.ones(num_diffusion_timesteps, dtype=np.float64)
  warmup_time = int(num_diffusion_timesteps * warmup_frac)
  betas[:warmup_time] = np.linspace(beta_start, beta_end, warmup_time, dtype=np.float64)
  return betas


def get_beta_schedule(beta_schedule, *, beta_start, beta_end, num_diffusion_timesteps):
  if beta_schedule == 'quad':
    betas = np.linspace(beta_start ** 0.5, beta_end ** 0.5, num_diffusion_timesteps, dtype=np.float64) ** 2
  elif beta_schedule == 'linear':
    betas = np.linspace(beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64)
  elif beta_schedule == 'warmup10':
    betas = _warmup_beta(beta_start, beta_end, num_diffusion_timesteps, 0.1)
  elif beta_schedule == 'warmup50':
    betas = _warmup_beta(beta_start, beta_end, num_diffusion_timesteps, 0.5)
  elif beta_schedule == 'const':
    betas = beta_end * np.ones(num_diffusion_timesteps, dtype=np.float64)
  elif beta_schedule == 'jsd':  # 1/T, 1/(T-1), 1/(T-2), ..., 1
    betas = 1. / np.linspace(num_diffusion_timesteps, 1, num_diffusion_timesteps, dtype=np.float64)
  else:
    raise NotImplementedError(beta_schedule)
  assert betas.shape == (num_diffusion_timesteps,)
  return betas


class GaussianDiffusion2:
  """
  Contains utilities for the diffusion model.

  Arguments:
  - what the network predicts (x_{t-1}, x_0, or epsilon)
  - which loss function (kl or unweighted MSE)
  - what is the variance of p(x_{t-1}|x_t) (learned, fixed to beta, or fixed to weighted beta)
  - what type of decoder, and how to weight its loss? is its variance learned too?
  """

  def __init__(self, *, betas, model_mean_type, model_var_type, loss_type):
    self.model_mean_type = model_mean_type  # xprev, xstart, eps
    self.model_var_type = model_var_type  # learned, fixedsmall, fixedlarge
    self.loss_type = loss_type  # kl, mse

    assert isinstance(betas, np.ndarray)
    self.betas = betas = betas.astype(np.float64)  # computations here in float64 for accuracy
    assert (betas > 0).all() and (betas <= 1).all()
    timesteps, = betas.shape
    self.num_timesteps = int(timesteps)

    alphas = 1. - betas
    self.alphas_cumprod = np.cumprod(alphas, axis=0)
    self.alphas_cumprod_prev = np.append(1., self.alphas_cumprod[:-1])
    assert self.alphas_cumprod_prev.shape == (timesteps,)

    # calculations for diffusion q(x_t | x_{t-1}) and others
    self.sqrt_alphas_cumprod = np.sqrt(self.alphas_cumprod)
    self.sqrt_one_minus_alphas_cumprod = np.sqrt(1. - self.alphas_cumprod)
    self.log_one_minus_alphas_cumprod = np.log(1. - self.alphas_cumprod)
    self.sqrt_recip_alphas_cumprod = np.sqrt(1. / self.alphas_cumprod)
    self.sqrt_recipm1_alphas_cumprod = np.sqrt(1. / self.alphas_cumprod - 1)

    # calculations for posterior q(x_{t-1} | x_t, x_0)
    self.posterior_variance = betas * (1. - self.alphas_cumprod_prev) / (1. - self.alphas_cumprod)
    # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
    self.posterior_log_variance_clipped = np.log(np.append(self.posterior_variance[1], self.posterior_variance[1:]))
    self.posterior_mean_coef1 = betas * np.sqrt(self.alphas_cumprod_prev) / (1. - self.alphas_cumprod)
    self.posterior_mean_coef2 = (1. - self.alphas_cumprod_prev) * np.sqrt(alphas) / (1. - self.alphas_cumprod)

  @staticmethod
  def _extract(a, t, x_shape):
    """
    Extract some coefficients at specified timesteps,
    then reshape to [batch_size, 1, 1, 1, 1, ...] for broadcasting purposes.
    """
    bs, = t.shape
    assert x_shape[0] == bs
    out = tf.gather(tf.convert_to_tensor(a, dtype=tf.float32), t)
    assert out.shape == [bs]
    return tf.reshape(out, [bs] + ((len(x_shape) - 1) * [1]))

  def q_mean_variance(self, x_start, t):
    mean = self._extract(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
    variance = self._extract(1. - self.alphas_cumprod, t, x_start.shape)
    log_variance = self._extract(self.log_one_minus_alphas_cumprod, t, x_start.shape)
    return mean, variance, log_variance

  def q_sample(self, x_start, t, noise=None):
    """
    Diffuse the data (t == 0 means diffused for 1 step)
    """
    if noise is None:
      noise = tf.random_normal(shape=x_start.shape)
    assert noise.shape == x_start.shape
    return (
        self._extract(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start +
        self._extract(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * noise
    )

  def q_posterior_mean_variance(self, x_start, x_t, t):
    """
    Compute the mean and variance of the diffusion posterior q(x_{t-1} | x_t, x_0)
    """
    assert x_start.shape == x_t.shape
    posterior_mean = (
        self._extract(self.posterior_mean_coef1, t, x_t.shape) * x_start +
        self._extract(self.posterior_mean_coef2, t, x_t.shape) * x_t
    )
    posterior_variance = self._extract(self.posterior_variance, t, x_t.shape)
    posterior_log_variance_clipped = self._extract(self.posterior_log_variance_clipped, t, x_t.shape)
    assert (posterior_mean.shape[0] == posterior_variance.shape[0] == posterior_log_variance_clipped.shape[0] ==
            x_start.shape[0])
    return posterior_mean, posterior_variance, posterior_log_variance_clipped

  def p_mean_variance(self, denoise_fn, *, x, t, clip_denoised: bool, return_pred_xstart: bool):
    B, H, W, C = x.shape
    assert t.shape == [B]
    model_output = denoise_fn(x, t)

    # Learned or fixed variance?
    if self.model_var_type == 'learned':
      assert model_output.shape == [B, H, W, C * 2]
      model_output, model_log_variance = tf.split(model_output, 2, axis=-1)
      model_variance = tf.exp(model_log_variance)
    elif self.model_var_type in ['fixedsmall', 'fixedlarge']:
      # below: only log_variance is used in the KL computations
      model_variance, model_log_variance = {
        # for fixedlarge, we set the initial (log-)variance like so to get a better decoder log likelihood
        'fixedlarge': (self.betas, np.log(np.append(self.posterior_variance[1], self.betas[1:]))),
        'fixedsmall': (self.posterior_variance, self.posterior_log_variance_clipped),
      }[self.model_var_type]
      model_variance = self._extract(model_variance, t, x.shape) * tf.ones(x.shape.as_list())
      model_log_variance = self._extract(model_log_variance, t, x.shape) * tf.ones(x.shape.as_list())
    else:
      raise NotImplementedError(self.model_var_type)

    # Mean parameterization
    _maybe_clip = lambda x_: (tf.clip_by_value(x_, -1., 1.) if clip_denoised else x_)
    if self.model_mean_type == 'xprev':  # the model predicts x_{t-1}
      pred_xstart = _maybe_clip(self._predict_xstart_from_xprev(x_t=x, t=t, xprev=model_output))
      model_mean = model_output
    elif self.model_mean_type == 'xstart':  # the model predicts x_0
      pred_xstart = _maybe_clip(model_output)
      model_mean, _, _ = self.q_posterior_mean_variance(x_start=pred_xstart, x_t=x, t=t)
    elif self.model_mean_type == 'eps':  # the model predicts epsilon
      pred_xstart = _maybe_clip(self._predict_xstart_from_eps(x_t=x, t=t, eps=model_output))
      model_mean, _, _ = self.q_posterior_mean_variance(x_start=pred_xstart, x_t=x, t=t)
    else:
      raise NotImplementedError(self.model_mean_type)

    assert model_mean.shape == model_log_variance.shape == pred_xstart.shape == x.shape
    if return_pred_xstart:
      return model_mean, model_variance, model_log_variance, pred_xstart
    else:
      return model_mean, model_variance, model_log_variance

  def _predict_xstart_from_eps(self, x_t, t, eps):
    assert x_t.shape == eps.shape
    return (
        self._extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t -
        self._extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * eps
    )

  def _predict_xstart_from_xprev(self, x_t, t, xprev):
    assert x_t.shape == xprev.shape
    return (  # (xprev - coef2*x_t) / coef1
        self._extract(1. / self.posterior_mean_coef1, t, x_t.shape) * xprev -
        self._extract(self.posterior_mean_coef2 / self.posterior_mean_coef1, t, x_t.shape) * x_t
    )

  # === Sampling ===

  def p_sample(self, denoise_fn, *, x, t, noise_fn, clip_denoised=True, return_pred_xstart: bool):
    """
    Sample from the model
    """
    model_mean, _, model_log_variance, pred_xstart = self.p_mean_variance(
      denoise_fn, x=x, t=t, clip_denoised=clip_denoised, return_pred_xstart=True)
    noise = noise_fn(shape=x.shape, dtype=x.dtype)
    assert noise.shape == x.shape
    # no noise when t == 0
    nonzero_mask = tf.reshape(1 - tf.cast(tf.equal(t, 0), tf.float32), [x.shape[0]] + [1] * (len(x.shape) - 1))
    sample = model_mean + nonzero_mask * tf.exp(0.5 * model_log_variance) * noise
    assert sample.shape == pred_xstart.shape
    return (sample, pred_xstart) if return_pred_xstart else sample

  def p_sample_loop(self, denoise_fn, *, shape, noise_fn=tf.random_normal):
    """
    Generate samples
    """
    assert isinstance(shape, (tuple, list))
    i_0 = tf.constant(self.num_timesteps - 1, dtype=tf.int32)
    img_0 = noise_fn(shape=shape, dtype=tf.float32)
    _, img_final = tf.while_loop(
      cond=lambda i_, _: tf.greater_equal(i_, 0),
      body=lambda i_, img_: [
        i_ - 1,
        self.p_sample(
          denoise_fn=denoise_fn, x=img_, t=tf.fill([shape[0]], i_), noise_fn=noise_fn, return_pred_xstart=False)
      ],
      loop_vars=[i_0, img_0],
      shape_invariants=[i_0.shape, img_0.shape],
      back_prop=False
    )
    assert img_final.shape == shape
    return img_final

  def p_sample_loop_progressive(self, denoise_fn, *, shape, noise_fn=tf.random_normal, include_xstartpred_freq=50):
    """
    Generate samples and keep track of prediction of x0
    """
    assert isinstance(shape, (tuple, list))
    i_0 = tf.constant(self.num_timesteps - 1, dtype=tf.int32)
    img_0 = noise_fn(shape=shape, dtype=tf.float32)  # [B, H, W, C]

    num_recorded_xstartpred = self.num_timesteps // include_xstartpred_freq
    xstartpreds_0 = tf.zeros([shape[0], num_recorded_xstartpred, *shape[1:]], dtype=tf.float32)  # [B, N, H, W, C]

    def _loop_body(i_, img_, xstartpreds_):
      # Sample p(x_{t-1} | x_t) as usual
      sample, pred_xstart = self.p_sample(
        denoise_fn=denoise_fn, x=img_, t=tf.fill([shape[0]], i_), noise_fn=noise_fn, return_pred_xstart=True)
      assert sample.shape == pred_xstart.shape == shape
      # Keep track of prediction of x0
      insert_mask = tf.equal(tf.floordiv(i_, include_xstartpred_freq),
                             tf.range(num_recorded_xstartpred, dtype=tf.int32))
      insert_mask = tf.reshape(tf.cast(insert_mask, dtype=tf.float32),
                               [1, num_recorded_xstartpred, *([1] * len(shape[1:]))])  # [1, N, 1, 1, 1]
      new_xstartpreds = insert_mask * pred_xstart[:, None, ...] + (1. - insert_mask) * xstartpreds_
      return [i_ - 1, sample, new_xstartpreds]

    _, img_final, xstartpreds_final = tf.while_loop(
      cond=lambda i_, img_, xstartpreds_: tf.greater_equal(i_, 0),
      body=_loop_body,
      loop_vars=[i_0, img_0, xstartpreds_0],
      shape_invariants=[i_0.shape, img_0.shape, xstartpreds_0.shape],
      back_prop=False
    )
    assert img_final.shape == shape and xstartpreds_final.shape == xstartpreds_0.shape
    return img_final, xstartpreds_final  # xstart predictions should agree with img_final at step 0

  # === Log likelihood calculation ===

  def _vb_terms_bpd(self, denoise_fn, x_start, x_t, t, *, clip_denoised: bool, return_pred_xstart: bool):
    true_mean, _, true_log_variance_clipped = self.q_posterior_mean_variance(x_start=x_start, x_t=x_t, t=t)
    model_mean, _, model_log_variance, pred_xstart = self.p_mean_variance(
      denoise_fn, x=x_t, t=t, clip_denoised=clip_denoised, return_pred_xstart=True)
    kl = normal_kl(true_mean, true_log_variance_clipped, model_mean, model_log_variance)
    kl = nn.meanflat(kl) / np.log(2.)

    decoder_nll = -utils.discretized_gaussian_log_likelihood(
      x_start, means=model_mean, log_scales=0.5 * model_log_variance)
    assert decoder_nll.shape == x_start.shape
    decoder_nll = nn.meanflat(decoder_nll) / np.log(2.)

    # At the first timestep return the decoder NLL, otherwise return KL(q(x_{t-1}|x_t,x_0) || p(x_{t-1}|x_t))
    assert kl.shape == decoder_nll.shape == t.shape == [x_start.shape[0]]
    output = tf.where(tf.equal(t, 0), decoder_nll, kl)
    return (output, pred_xstart) if return_pred_xstart else output

  def training_losses(self, denoise_fn, x_start, t, noise=None):
    """
    Training loss calculation
    """

    # Add noise to data
    assert t.shape == [x_start.shape[0]]
    if noise is None:
      noise = tf.random_normal(shape=x_start.shape, dtype=x_start.dtype)
    assert noise.shape == x_start.shape and noise.dtype == x_start.dtype
    x_t = self.q_sample(x_start=x_start, t=t, noise=noise)

    # Calculate the loss
    if self.loss_type == 'kl':  # the variational bound
      losses = self._vb_terms_bpd(
        denoise_fn=denoise_fn, x_start=x_start, x_t=x_t, t=t, clip_denoised=False, return_pred_xstart=False)
    elif self.loss_type == 'mse':  # unweighted MSE
      assert self.model_var_type != 'learned'
      target = {
        'xprev': self.q_posterior_mean_variance(x_start=x_start, x_t=x_t, t=t)[0],
        'xstart': x_start,
        'eps': noise
      }[self.model_mean_type]
      model_output = denoise_fn(x_t, t)
      assert model_output.shape == target.shape == x_start.shape
      losses = nn.meanflat(tf.squared_difference(target, model_output))
    else:
      raise NotImplementedError(self.loss_type)

    assert losses.shape == t.shape
    return losses

  def _prior_bpd(self, x_start):
    B, T = x_start.shape[0], self.num_timesteps
    qt_mean, _, qt_log_variance = self.q_mean_variance(x_start, t=tf.fill([B], tf.constant(T - 1, dtype=tf.int32)))
    kl_prior = normal_kl(mean1=qt_mean, logvar1=qt_log_variance, mean2=0., logvar2=0.)
    assert kl_prior.shape == x_start.shape
    return nn.meanflat(kl_prior) / np.log(2.)

  def calc_bpd_loop(self, denoise_fn, x_start, *, clip_denoised=True):
    (B, H, W, C), T = x_start.shape, self.num_timesteps

    def _loop_body(t_, cur_vals_bt_, cur_mse_bt_):
      assert t_.shape == []
      t_b = tf.fill([B], t_)
      # Calculate VLB term at the current timestep
      new_vals_b, pred_xstart = self._vb_terms_bpd(
        denoise_fn, x_start=x_start, x_t=self.q_sample(x_start=x_start, t=t_b), t=t_b,
        clip_denoised=clip_denoised, return_pred_xstart=True)
      # MSE for progressive prediction loss
      assert pred_xstart.shape == x_start.shape
      new_mse_b = nn.meanflat(tf.squared_difference(pred_xstart, x_start))
      assert new_vals_b.shape == new_mse_b.shape == [B]
      # Insert the calculated term into the tensor of all terms
      mask_bt = tf.cast(tf.equal(t_b[:, None], tf.range(T)[None, :]), dtype=tf.float32)
      new_vals_bt = cur_vals_bt_ * (1. - mask_bt) + new_vals_b[:, None] * mask_bt
      new_mse_bt = cur_mse_bt_ * (1. - mask_bt) + new_mse_b[:, None] * mask_bt
      assert mask_bt.shape == cur_vals_bt_.shape == new_vals_bt.shape == [B, T]
      return t_ - 1, new_vals_bt, new_mse_bt

    t_0 = tf.constant(T - 1, dtype=tf.int32)
    terms_0 = tf.zeros([B, T])
    mse_0 = tf.zeros([B, T])
    _, terms_bpd_bt, mse_bt = tf.while_loop(  # Note that this can be implemented with tf.map_fn instead
      cond=lambda t_, cur_vals_bt_, cur_mse_bt_: tf.greater_equal(t_, 0),
      body=_loop_body,
      loop_vars=[t_0, terms_0, mse_0],
      shape_invariants=[t_0.shape, terms_0.shape, mse_0.shape],
      back_prop=False
    )
    prior_bpd_b = self._prior_bpd(x_start)
    total_bpd_b = tf.reduce_sum(terms_bpd_bt, axis=1) + prior_bpd_b
    assert terms_bpd_bt.shape == mse_bt.shape == [B, T] and total_bpd_b.shape == prior_bpd_b.shape == [B]
    return total_bpd_b, terms_bpd_bt, prior_bpd_b, mse_bt
"""
This code started out as a PyTorch port of Ho et al's diffusion models:
https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/diffusion_utils_2.py

Docstrings have been added, as well as DDIM sampling and a new collection of beta schedules.
"""

import enum
import math

import numpy as np
import torch as th

from .nn import mean_flat
from .losses import normal_kl, discretized_gaussian_log_likelihood


def get_named_beta_schedule(schedule_name, num_diffusion_timesteps):
    """
    Get a pre-defined beta schedule for the given name.

    The beta schedule library consists of beta schedules which remain similar
    in the limit of num_diffusion_timesteps.
    Beta schedules may be added, but should not be removed or changed once
    they are committed to maintain backwards compatibility.
    """
    if schedule_name == "linear":
        # Linear schedule from Ho et al, extended to work for any number of
        # diffusion steps.
        scale = 1000 / num_diffusion_timesteps
        beta_start = scale * 0.0001
        beta_end = scale * 0.02
        return np.linspace(
            beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64
        )
    elif schedule_name == "cosine":
        return betas_for_alpha_bar(
            num_diffusion_timesteps,
            lambda t: math.cos((t + 0.008) / 1.008 * math.pi / 2) ** 2,
        )
    else:
        raise NotImplementedError(f"unknown beta schedule: {schedule_name}")


def betas_for_alpha_bar(num_diffusion_timesteps, alpha_bar, max_beta=0.999):
    """
    Create a beta schedule that discretizes the given alpha_t_bar function,
    which defines the cumulative product of (1-beta) over time from t = [0,1].

    :param num_diffusion_timesteps: the number of betas to produce.
    :param alpha_bar: a lambda that takes an argument t from 0 to 1 and
                      produces the cumulative product of (1-beta) up to that
                      part of the diffusion process.
    :param max_beta: the maximum beta to use; use values lower than 1 to
                     prevent singularities.
    """
    betas = []
    for i in range(num_diffusion_timesteps):
        t1 = i / num_diffusion_timesteps
        t2 = (i + 1) / num_diffusion_timesteps
        betas.append(min(1 - alpha_bar(t2) / alpha_bar(t1), max_beta))
    return np.array(betas)


class ModelMeanType(enum.Enum):
    """
    Which type of output the model predicts.
    """

    PREVIOUS_X = enum.auto()  # the model predicts x_{t-1}
    START_X = enum.auto()  # the model predicts x_0
    EPSILON = enum.auto()  # the model predicts epsilon


class ModelVarType(enum.Enum):
    """
    What is used as the model's output variance.

    The LEARNED_RANGE option has been added to allow the model to predict
    values between FIXED_SMALL and FIXED_LARGE, making its job easier.
    """

    LEARNED = enum.auto()
    FIXED_SMALL = enum.auto()
    FIXED_LARGE = enum.auto()
    LEARNED_RANGE = enum.auto()


class LossType(enum.Enum):
    MSE = enum.auto()  # use raw MSE loss (and KL when learning variances)
    RESCALED_MSE = (
        enum.auto()
    )  # use raw MSE loss (with RESCALED_KL when learning variances)
    KL = enum.auto()  # use the variational lower-bound
    RESCALED_KL = enum.auto()  # like KL, but rescale to estimate the full VLB

    def is_vb(self):
        return self == LossType.KL or self == LossType.RESCALED_KL


class GaussianDiffusion:
    """
    Utilities for training and sampling diffusion models.

    Ported directly from here, and then adapted over time to further experimentation.
    https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/diffusion_utils_2.py#L42

    :param betas: a 1-D numpy array of betas for each diffusion timestep,
                  starting at T and going to 1.
    :param model_mean_type: a ModelMeanType determining what the model outputs.
    :param model_var_type: a ModelVarType determining how variance is output.
    :param loss_type: a LossType determining the loss function to use.
    :param rescale_timesteps: if True, pass floating point timesteps into the
                              model so that they are always scaled like in the
                              original paper (0 to 1000).
    """

    def __init__(
        self,
        *,
        betas,
        model_mean_type,
        model_var_type,
        loss_type,
        rescale_timesteps=False,
    ):
        self.model_mean_type = model_mean_type
        self.model_var_type = model_var_type
        self.loss_type = loss_type
        self.rescale_timesteps = rescale_timesteps

        # Use float64 for accuracy.
        betas = np.array(betas, dtype=np.float64)
        self.betas = betas
        assert len(betas.shape) == 1, "betas must be 1-D"
        assert (betas > 0).all() and (betas <= 1).all()

        self.num_timesteps = int(betas.shape[0])

        alphas = 1.0 - betas
        self.alphas_cumprod = np.cumprod(alphas, axis=0)
        self.alphas_cumprod_prev = np.append(1.0, self.alphas_cumprod[:-1])
        self.alphas_cumprod_next = np.append(self.alphas_cumprod[1:], 0.0)
        assert self.alphas_cumprod_prev.shape == (self.num_timesteps,)

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.sqrt_alphas_cumprod = np.sqrt(self.alphas_cumprod)
        self.sqrt_one_minus_alphas_cumprod = np.sqrt(1.0 - self.alphas_cumprod)
        self.log_one_minus_alphas_cumprod = np.log(1.0 - self.alphas_cumprod)
        self.sqrt_recip_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod)
        self.sqrt_recipm1_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod - 1)

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        self.posterior_variance = (
            betas * (1.0 - self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        # log calculation clipped because the posterior variance is 0 at the
        # beginning of the diffusion chain.
        self.posterior_log_variance_clipped = np.log(
            np.append(self.posterior_variance[1], self.posterior_variance[1:])
        )
        self.posterior_mean_coef1 = (
            betas * np.sqrt(self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        self.posterior_mean_coef2 = (
            (1.0 - self.alphas_cumprod_prev)
            * np.sqrt(alphas)
            / (1.0 - self.alphas_cumprod)
        )

    def q_mean_variance(self, x_start, t):
        """
        Get the distribution q(x_t | x_0).

        :param x_start: the [N x C x ...] tensor of noiseless inputs.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :return: A tuple (mean, variance, log_variance), all of x_start's shape.
        """
        mean = (
            _extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
        )
        variance = _extract_into_tensor(1.0 - self.alphas_cumprod, t, x_start.shape)
        log_variance = _extract_into_tensor(
            self.log_one_minus_alphas_cumprod, t, x_start.shape
        )
        return mean, variance, log_variance

    def q_sample(self, x_start, t, noise=None):
        """
        Diffuse the data for a given number of diffusion steps.

        In other words, sample from q(x_t | x_0).

        :param x_start: the initial data batch.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :param noise: if specified, the split-out normal noise.
        :return: A noisy version of x_start.
        """
        if noise is None:
            noise = th.randn_like(x_start)
        assert noise.shape == x_start.shape
        return (
            _extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
            + _extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape)
            * noise
        )

    def q_posterior_mean_variance(self, x_start, x_t, t):
        """
        Compute the mean and variance of the diffusion posterior:

            q(x_{t-1} | x_t, x_0)

        """
        assert x_start.shape == x_t.shape
        posterior_mean = (
            _extract_into_tensor(self.posterior_mean_coef1, t, x_t.shape) * x_start
            + _extract_into_tensor(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = _extract_into_tensor(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = _extract_into_tensor(
            self.posterior_log_variance_clipped, t, x_t.shape
        )
        assert (
            posterior_mean.shape[0]
            == posterior_variance.shape[0]
            == posterior_log_variance_clipped.shape[0]
            == x_start.shape[0]
        )
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def p_mean_variance(
        self, model, x, t, clip_denoised=True, denoised_fn=None, model_kwargs=None
    ):
        """
        Apply the model to get p(x_{t-1} | x_t), as well as a prediction of
        the initial x, x_0.

        :param model: the model, which takes a signal and a batch of timesteps
                      as input.
        :param x: the [N x C x ...] tensor at time t.
        :param t: a 1-D Tensor of timesteps.
        :param clip_denoised: if True, clip the denoised signal into [-1, 1].
        :param denoised_fn: if not None, a function which applies to the
            x_start prediction before it is used to sample. Applies before
            clip_denoised.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :return: a dict with the following keys:
                 - 'mean': the model mean output.
                 - 'variance': the model variance output.
                 - 'log_variance': the log of 'variance'.
                 - 'pred_xstart': the prediction for x_0.
        """
        if model_kwargs is None:
            model_kwargs = {}

        B, C = x.shape[:2]
        assert t.shape == (B,)
        model_output = model(x, self._scale_timesteps(t), **model_kwargs)

        if self.model_var_type in [ModelVarType.LEARNED, ModelVarType.LEARNED_RANGE]:
            assert model_output.shape == (B, C * 2, *x.shape[2:])
            model_output, model_var_values = th.split(model_output, C, dim=1)
            if self.model_var_type == ModelVarType.LEARNED:
                model_log_variance = model_var_values
                model_variance = th.exp(model_log_variance)
            else:
                min_log = _extract_into_tensor(
                    self.posterior_log_variance_clipped, t, x.shape
                )
                max_log = _extract_into_tensor(np.log(self.betas), t, x.shape)
                # The model_var_values is [-1, 1] for [min_var, max_var].
                frac = (model_var_values + 1) / 2
                model_log_variance = frac * max_log + (1 - frac) * min_log
                model_variance = th.exp(model_log_variance)
        else:
            model_variance, model_log_variance = {
                # for fixedlarge, we set the initial (log-)variance like so
                # to get a better decoder log likelihood.
                ModelVarType.FIXED_LARGE: (
                    np.append(self.posterior_variance[1], self.betas[1:]),
                    np.log(np.append(self.posterior_variance[1], self.betas[1:])),
                ),
                ModelVarType.FIXED_SMALL: (
                    self.posterior_variance,
                    self.posterior_log_variance_clipped,
                ),
            }[self.model_var_type]
            model_variance = _extract_into_tensor(model_variance, t, x.shape)
            model_log_variance = _extract_into_tensor(model_log_variance, t, x.shape)

        def process_xstart(x):
            if denoised_fn is not None:
                x = denoised_fn(x)
            if clip_denoised:
                return x.clamp(-1, 1)
            return x

        if self.model_mean_type == ModelMeanType.PREVIOUS_X:
            pred_xstart = process_xstart(
                self._predict_xstart_from_xprev(x_t=x, t=t, xprev=model_output)
            )
            model_mean = model_output
        elif self.model_mean_type in [ModelMeanType.START_X, ModelMeanType.EPSILON]:
            if self.model_mean_type == ModelMeanType.START_X:
                pred_xstart = process_xstart(model_output)
            else:
                pred_xstart = process_xstart(
                    self._predict_xstart_from_eps(x_t=x, t=t, eps=model_output)
                )
            model_mean, _, _ = self.q_posterior_mean_variance(
                x_start=pred_xstart, x_t=x, t=t
            )
        else:
            raise NotImplementedError(self.model_mean_type)

        assert (
            model_mean.shape == model_log_variance.shape == pred_xstart.shape == x.shape
        )
        return {
            "mean": model_mean,
            "variance": model_variance,
            "log_variance": model_log_variance,
            "pred_xstart": pred_xstart,
        }

    def _predict_xstart_from_eps(self, x_t, t, eps):
        assert x_t.shape == eps.shape
        return (
            _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t
            - _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * eps
        )

    def _predict_xstart_from_xprev(self, x_t, t, xprev):
        assert x_t.shape == xprev.shape
        return (  # (xprev - coef2*x_t) / coef1
            _extract_into_tensor(1.0 / self.posterior_mean_coef1, t, x_t.shape) * xprev
            - _extract_into_tensor(
                self.posterior_mean_coef2 / self.posterior_mean_coef1, t, x_t.shape
            )
            * x_t
        )

    def _predict_eps_from_xstart(self, x_t, t, pred_xstart):
        return (
            _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t
            - pred_xstart
        ) / _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)

    def _scale_timesteps(self, t):
        if self.rescale_timesteps:
            return t.float() * (1000.0 / self.num_timesteps)
        return t

    def condition_mean(self, cond_fn, p_mean_var, x, t, model_kwargs=None):
        """
        Compute the mean for the previous step, given a function cond_fn that
        computes the gradient of a conditional log probability with respect to
        x. In particular, cond_fn computes grad(log(p(y|x))), and we want to
        condition on y.

        This uses the conditioning strategy from Sohl-Dickstein et al. (2015).
        """
        gradient = cond_fn(x, self._scale_timesteps(t), **model_kwargs)
        new_mean = (
            p_mean_var["mean"].float() + p_mean_var["variance"] * gradient.float()
        )
        return new_mean

    def condition_score(self, cond_fn, p_mean_var, x, t, model_kwargs=None):
        """
        Compute what the p_mean_variance output would have been, should the
        model's score function be conditioned by cond_fn.

        See condition_mean() for details on cond_fn.

        Unlike condition_mean(), this instead uses the conditioning strategy
        from Song et al (2020).
        """
        alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, x.shape)

        eps = self._predict_eps_from_xstart(x, t, p_mean_var["pred_xstart"])
        eps = eps - (1 - alpha_bar).sqrt() * cond_fn(
            x, self._scale_timesteps(t), **model_kwargs
        )

        out = p_mean_var.copy()
        out["pred_xstart"] = self._predict_xstart_from_eps(x, t, eps)
        out["mean"], _, _ = self.q_posterior_mean_variance(
            x_start=out["pred_xstart"], x_t=x, t=t
        )
        return out

    def p_sample(
        self,
        model,
        x,
        t,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
    ):
        """
        Sample x_{t-1} from the model at the given timestep.

        :param model: the model to sample from.
        :param x: the current tensor at x_{t-1}.
        :param t: the value of t, starting at 0 for the first diffusion step.
        :param clip_denoised: if True, clip the x_start prediction to [-1, 1].
        :param denoised_fn: if not None, a function which applies to the
            x_start prediction before it is used to sample.
        :param cond_fn: if not None, this is a gradient function that acts
                        similarly to the model.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :return: a dict containing the following keys:
                 - 'sample': a random sample from the model.
                 - 'pred_xstart': a prediction of x_0.
        """
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        noise = th.randn_like(x)
        nonzero_mask = (
            (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))
        )  # no noise when t == 0
        if cond_fn is not None:
            out["mean"] = self.condition_mean(
                cond_fn, out, x, t, model_kwargs=model_kwargs
            )
        sample = out["mean"] + nonzero_mask * th.exp(0.5 * out["log_variance"]) * noise
        return {"sample": sample, "pred_xstart": out["pred_xstart"]}

    def p_sample_loop(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
    ):
        """
        Generate samples from the model.

        :param model: the model module.
        :param shape: the shape of the samples, (N, C, H, W).
        :param noise: if specified, the noise from the encoder to sample.
                      Should be of the same shape as `shape`.
        :param clip_denoised: if True, clip x_start predictions to [-1, 1].
        :param denoised_fn: if not None, a function which applies to the
            x_start prediction before it is used to sample.
        :param cond_fn: if not None, this is a gradient function that acts
                        similarly to the model.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :param device: if specified, the device to create the samples on.
                       If not specified, use a model parameter's device.
        :param progress: if True, show a tqdm progress bar.
        :return: a non-differentiable batch of samples.
        """
        final = None
        for sample in self.p_sample_loop_progressive(
            model,
            shape,
            noise=noise,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
        ):
            final = sample
        return final["sample"]

    def p_sample_loop_progressive(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
    ):
        """
        Generate samples from the model and yield intermediate samples from
        each timestep of diffusion.

        Arguments are the same as p_sample_loop().
        Returns a generator over dicts, where each dict is the return value of
        p_sample().
        """
        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        if noise is not None:
            img = noise
        else:
            img = th.randn(*shape, device=device)
        indices = list(range(self.num_timesteps))[::-1]

        if progress:
            # Lazy import so that we don't depend on tqdm.
            from tqdm.auto import tqdm

            indices = tqdm(indices)

        for i in indices:
            t = th.tensor([i] * shape[0], device=device)
            with th.no_grad():
                out = self.p_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                )
                yield out
                img = out["sample"]

    def ddim_sample(
        self,
        model,
        x,
        t,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        eta=0.0,
    ):
        """
        Sample x_{t-1} from the model using DDIM.

        Same usage as p_sample().
        """
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        if cond_fn is not None:
            out = self.condition_score(cond_fn, out, x, t, model_kwargs=model_kwargs)

        # Usually our model outputs epsilon, but we re-derive it
        # in case we used x_start or x_prev prediction.
        eps = self._predict_eps_from_xstart(x, t, out["pred_xstart"])

        alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, x.shape)
        alpha_bar_prev = _extract_into_tensor(self.alphas_cumprod_prev, t, x.shape)
        sigma = (
            eta
            * th.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
            * th.sqrt(1 - alpha_bar / alpha_bar_prev)
        )
        # Equation 12.
        noise = th.randn_like(x)
        mean_pred = (
            out["pred_xstart"] * th.sqrt(alpha_bar_prev)
            + th.sqrt(1 - alpha_bar_prev - sigma ** 2) * eps
        )
        nonzero_mask = (
            (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))
        )  # no noise when t == 0
        sample = mean_pred + nonzero_mask * sigma * noise
        return {"sample": sample, "pred_xstart": out["pred_xstart"]}

    def ddim_reverse_sample(
        self,
        model,
        x,
        t,
        clip_denoised=True,
        denoised_fn=None,
        model_kwargs=None,
        eta=0.0,
    ):
        """
        Sample x_{t+1} from the model using DDIM reverse ODE.
        """
        assert eta == 0.0, "Reverse ODE only for deterministic path"
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        # Usually our model outputs epsilon, but we re-derive it
        # in case we used x_start or x_prev prediction.
        eps = (
            _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x.shape) * x
            - out["pred_xstart"]
        ) / _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x.shape)
        alpha_bar_next = _extract_into_tensor(self.alphas_cumprod_next, t, x.shape)

        # Equation 12. reversed
        mean_pred = (
            out["pred_xstart"] * th.sqrt(alpha_bar_next)
            + th.sqrt(1 - alpha_bar_next) * eps
        )

        return {"sample": mean_pred, "pred_xstart": out["pred_xstart"]}

    def ddim_sample_loop(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        eta=0.0,
    ):
        """
        Generate samples from the model using DDIM.

        Same usage as p_sample_loop().
        """
        final = None
        for sample in self.ddim_sample_loop_progressive(
            model,
            shape,
            noise=noise,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
            eta=eta,
        ):
            final = sample
        return final["sample"]

    def ddim_sample_loop_progressive(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        eta=0.0,
    ):
        """
        Use DDIM to sample from the model and yield intermediate samples from
        each timestep of DDIM.

        Same usage as p_sample_loop_progressive().
        """
        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        if noise is not None:
            img = noise
        else:
            img = th.randn(*shape, device=device)
        indices = list(range(self.num_timesteps))[::-1]

        if progress:
            # Lazy import so that we don't depend on tqdm.
            from tqdm.auto import tqdm

            indices = tqdm(indices)

        for i in indices:
            t = th.tensor([i] * shape[0], device=device)
            with th.no_grad():
                out = self.ddim_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                    eta=eta,
                )
                yield out
                img = out["sample"]

    def _vb_terms_bpd(
        self, model, x_start, x_t, t, clip_denoised=True, model_kwargs=None
    ):
        """
        Get a term for the variational lower-bound.

        The resulting units are bits (rather than nats, as one might expect).
        This allows for comparison to other papers.

        :return: a dict with the following keys:
                 - 'output': a shape [N] tensor of NLLs or KLs.
                 - 'pred_xstart': the x_0 predictions.
        """
        true_mean, _, true_log_variance_clipped = self.q_posterior_mean_variance(
            x_start=x_start, x_t=x_t, t=t
        )
        out = self.p_mean_variance(
            model, x_t, t, clip_denoised=clip_denoised, model_kwargs=model_kwargs
        )
        kl = normal_kl(
            true_mean, true_log_variance_clipped, out["mean"], out["log_variance"]
        )
        kl = mean_flat(kl) / np.log(2.0)

        decoder_nll = -discretized_gaussian_log_likelihood(
            x_start, means=out["mean"], log_scales=0.5 * out["log_variance"]
        )
        assert decoder_nll.shape == x_start.shape
        decoder_nll = mean_flat(decoder_nll) / np.log(2.0)

        # At the first timestep return the decoder NLL,
        # otherwise return KL(q(x_{t-1}|x_t,x_0) || p(x_{t-1}|x_t))
        output = th.where((t == 0), decoder_nll, kl)
        return {"output": output, "pred_xstart": out["pred_xstart"]}

    def training_losses(self, model, x_start, t, model_kwargs=None, noise=None):
        """
        Compute training losses for a single timestep.

        :param model: the model to evaluate loss on.
        :param x_start: the [N x C x ...] tensor of inputs.
        :param t: a batch of timestep indices.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :param noise: if specified, the specific Gaussian noise to try to remove.
        :return: a dict with the key "loss" containing a tensor of shape [N].
                 Some mean or variance settings may also have other keys.
        """
        if model_kwargs is None:
            model_kwargs = {}
        if noise is None:
            noise = th.randn_like(x_start)
        x_t = self.q_sample(x_start, t, noise=noise)

        terms = {}

        if self.loss_type == LossType.KL or self.loss_type == LossType.RESCALED_KL:
            terms["loss"] = self._vb_terms_bpd(
                model=model,
                x_start=x_start,
                x_t=x_t,
                t=t,
                clip_denoised=False,
                model_kwargs=model_kwargs,
            )["output"]
            if self.loss_type == LossType.RESCALED_KL:
                terms["loss"] *= self.num_timesteps
        elif self.loss_type == LossType.MSE or self.loss_type == LossType.RESCALED_MSE:
            model_output = model(x_t, self._scale_timesteps(t), **model_kwargs)

            if self.model_var_type in [
                ModelVarType.LEARNED,
                ModelVarType.LEARNED_RANGE,
            ]:
                B, C = x_t.shape[:2]
                assert model_output.shape == (B, C * 2, *x_t.shape[2:])
                model_output, model_var_values = th.split(model_output, C, dim=1)
                # Learn the variance using the variational bound, but don't let
                # it affect our mean prediction.
                frozen_out = th.cat([model_output.detach(), model_var_values], dim=1)
                terms["vb"] = self._vb_terms_bpd(
                    model=lambda *args, r=frozen_out: r,
                    x_start=x_start,
                    x_t=x_t,
                    t=t,
                    clip_denoised=False,
                )["output"]
                if self.loss_type == LossType.RESCALED_MSE:
                    # Divide by 1000 for equivalence with initial implementation.
                    # Without a factor of 1/1000, the VB term hurts the MSE term.
                    terms["vb"] *= self.num_timesteps / 1000.0

            target = {
                ModelMeanType.PREVIOUS_X: self.q_posterior_mean_variance(
                    x_start=x_start, x_t=x_t, t=t
                )[0],
                ModelMeanType.START_X: x_start,
                ModelMeanType.EPSILON: noise,
            }[self.model_mean_type]
            assert model_output.shape == target.shape == x_start.shape
            terms["mse"] = mean_flat((target - model_output) ** 2)
            if "vb" in terms:
                terms["loss"] = terms["mse"] + terms["vb"]
            else:
                terms["loss"] = terms["mse"]
        else:
            raise NotImplementedError(self.loss_type)

        return terms

    def _prior_bpd(self, x_start):
        """
        Get the prior KL term for the variational lower-bound, measured in
        bits-per-dim.

        This term can't be optimized, as it only depends on the encoder.

        :param x_start: the [N x C x ...] tensor of inputs.
        :return: a batch of [N] KL values (in bits), one per batch element.
        """
        batch_size = x_start.shape[0]
        t = th.tensor([self.num_timesteps - 1] * batch_size, device=x_start.device)
        qt_mean, _, qt_log_variance = self.q_mean_variance(x_start, t)
        kl_prior = normal_kl(
            mean1=qt_mean, logvar1=qt_log_variance, mean2=0.0, logvar2=0.0
        )
        return mean_flat(kl_prior) / np.log(2.0)

    def calc_bpd_loop(self, model, x_start, clip_denoised=True, model_kwargs=None):
        """
        Compute the entire variational lower-bound, measured in bits-per-dim,
        as well as other related quantities.

        :param model: the model to evaluate loss on.
        :param x_start: the [N x C x ...] tensor of inputs.
        :param clip_denoised: if True, clip denoised samples.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.

        :return: a dict containing the following keys:
                 - total_bpd: the total variational lower-bound, per batch element.
                 - prior_bpd: the prior term in the lower-bound.
                 - vb: an [N x T] tensor of terms in the lower-bound.
                 - xstart_mse: an [N x T] tensor of x_0 MSEs for each timestep.
                 - mse: an [N x T] tensor of epsilon MSEs for each timestep.
        """
        device = x_start.device
        batch_size = x_start.shape[0]

        vb = []
        xstart_mse = []
        mse = []
        for t in list(range(self.num_timesteps))[::-1]:
            t_batch = th.tensor([t] * batch_size, device=device)
            noise = th.randn_like(x_start)
            x_t = self.q_sample(x_start=x_start, t=t_batch, noise=noise)
            # Calculate VLB term at the current timestep
            with th.no_grad():
                out = self._vb_terms_bpd(
                    model,
                    x_start=x_start,
                    x_t=x_t,
                    t=t_batch,
                    clip_denoised=clip_denoised,
                    model_kwargs=model_kwargs,
                )
            vb.append(out["output"])
            xstart_mse.append(mean_flat((out["pred_xstart"] - x_start) ** 2))
            eps = self._predict_eps_from_xstart(x_t, t_batch, out["pred_xstart"])
            mse.append(mean_flat((eps - noise) ** 2))

        vb = th.stack(vb, dim=1)
        xstart_mse = th.stack(xstart_mse, dim=1)
        mse = th.stack(mse, dim=1)

        prior_bpd = self._prior_bpd(x_start)
        total_bpd = vb.sum(dim=1) + prior_bpd
        return {
            "total_bpd": total_bpd,
            "prior_bpd": prior_bpd,
            "vb": vb,
            "xstart_mse": xstart_mse,
            "mse": mse,
        }


def _extract_into_tensor(arr, timesteps, broadcast_shape):
    """
    Extract values from a 1-D numpy array for a batch of indices.

    :param arr: the 1-D numpy array.
    :param timesteps: a tensor of indices into the array to extract.
    :param broadcast_shape: a larger shape of K dimensions with the batch
                            dimension equal to the length of timesteps.
    :return: a tensor of shape [batch_size, 1, ...] where the shape has K dims.
    """
    res = th.from_numpy(arr).to(device=timesteps.device)[timesteps].float()
    while len(res.shape) < len(broadcast_shape):
        res = res[..., None]
    return res.expand(broadcast_shape)
