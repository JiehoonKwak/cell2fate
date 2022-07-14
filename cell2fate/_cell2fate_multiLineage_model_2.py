from typing import List, Optional
from datetime import date
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from anndata import AnnData
from pyro import clear_param_store
from scvi import REGISTRY_KEYS
from scvi.data import AnnDataManager
from scvi.data.fields import (
    CategoricalObsField,
    LayerField,
    NumericalJointObsField,
    NumericalObsField,
)
from scvi.model.base import BaseModelClass, PyroSampleMixin, PyroSviTrainMixin
from scvi.utils import setup_anndata_dsp
from scipy.sparse import issparse

from cell2fate._pyro_base_cell2fate_module import Cell2FateBaseModule
from cell2fate._pyro_mixin import PltExportMixin, QuantileMixin
from ._cell2fate_singleLineage_module_5 import DifferentiationModel_OneLineage_DiscreteTwoStateTranscriptionRate
from cell2fate.utils import multiplot_from_generator

class Cell2fateModel(QuantileMixin, PyroSampleMixin, PyroSviTrainMixin, PltExportMixin, BaseModelClass):
    """
    Cell2fate model. User-end model class. See Module class for description of the model.

    Parameters
    ----------
    adata
        single-cell AnnData object that has been registered via :func:`~scvi.data.setup_anndata`
        and contains spliced and unspliced counts in adata.layers['spliced'], adata.layers['unspliced']
    **model_kwargs
        Keyword args for :class:`~scvi.external.LocationModelLinearDependentWMultiExperimentModel`

    Examples
    --------
    TODO add example
    >>>
    """

    def __init__(
        self,
        adata: AnnData,
        model_class=None,
        **model_kwargs,
    ):
        # in case any other model was created before that shares the same parameter names.
        clear_param_store()

        super().__init__(adata)

        if model_class is None:
            model_class = DifferentiationModel_OneLineage_DiscreteTwoStateTranscriptionRate

        # use per class average as initial value
#             model_kwargs["init_vals"] = {"per_cluster_mu_fg": aver.values.T.astype("float32") + 0.0001}

        self.module = Cell2FateBaseModule(
            model=model_class,
            n_obs=self.summary_stats["n_cells"],
            n_vars=self.summary_stats["n_vars"],
            n_batch=self.summary_stats["n_batch"],
            **model_kwargs,
        )
        self._model_summary_string = f'Differentiation model with the following params: \nn_batch: {self.summary_stats["n_batch"]} '
        self.init_params_ = self._get_init_params(locals())

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        layer: Optional[str] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        unspliced_label = 'unspliced',
        spliced_label = 'spliced',
        **kwargs,
    ):
        """
        %(summary)s.

        Parameters
        ----------
        %(param_layer)s
        %(param_batch_key)s
        %(param_labels_key)s
        """
        setup_method_args = cls._get_setup_method_args(**locals())
        adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
        anndata_fields = [
            LayerField('unspliced', unspliced_label, is_count_data=True),
            LayerField('spliced', spliced_label, is_count_data=True),
            CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
            NumericalObsField(REGISTRY_KEYS.INDICES_KEY, "_indices"),
        ]
        adata_manager = AnnDataManager(fields=anndata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

    def train(
        self,
        max_epochs: Optional[int] = None,
        batch_size: int = 2500,
        train_size: float = 1,
        lr: float = 0.002,
        **kwargs,
    ):
        """Train the model with useful defaults

        Parameters
        ----------
        max_epochs
            Number of passes through the dataset. If `None`, defaults to
            `np.min([round((20000 / n_cells) * 400), 400])`
        train_size
            Size of training set in the range [0.0, 1.0].
        batch_size
            Minibatch size to use during training. If `None`, no minibatching occurs and all
            data is copied to device (e.g., GPU).
        lr
            Optimiser learning rate (default optimiser is :class:`~pyro.optim.ClippedAdam`).
            Specifying optimiser via plan_kwargs overrides this choice of lr.
        kwargs
            Other arguments to scvi.model.base.PyroSviTrainMixin().train() method
        """
        
        self.max_epochs = max_epochs
        kwargs["max_epochs"] = max_epochs
        kwargs["batch_size"] = batch_size
        kwargs["train_size"] = train_size
        kwargs["lr"] = lr

        super().train(**kwargs)
        
    def _export2adata(self, samples):
        r"""
        Export key model variables and samples

        Parameters
        ----------
        samples
            dictionary with posterior mean, 5%/95% quantiles, SD, samples, generated by ``.sample_posterior()``

        Returns
        -------
            Updated dictionary with additional details is saved to ``adata.uns['mod']``.
        """
        # add factor filter and samples of all parameters to unstructured data
        results = {
            "model_name": str(self.module.__class__.__name__),
            "date": str(date.today()),
            "var_names": self.adata.var_names.tolist(),
            "obs_names": self.adata.obs_names.tolist(),
            "post_sample_means": samples["post_sample_means"],
            "post_sample_stds": samples["post_sample_stds"],
            "post_sample_q05": samples["post_sample_q05"],
            "post_sample_q95": samples["post_sample_q95"],
        }

        return results
    
    def normalize(self, samples, adata_manager, adata):
        r"""Normalise expression data by estimated technical variables.

        Parameters
        ----------
        samples
            dictionary with values of the posterior
        adata
            registered anndata
        Returns
        ---------
        adata with normalized counts added to adata.layers['unspliced_norm'], ['spliced_norm']
        """

        obs2sample = adata_manager.get_from_registry(REGISTRY_KEYS.BATCH_KEY)
        obs2sample = pd.get_dummies(obs2sample.flatten())

        unspliced_corrected = adata_manager.get_from_registry('unspliced')/samples["detection_y_cu"][0,:,:] - np.dot(obs2sample, samples["s_g_gene_add"][:,:,0])
        adata.layers['unspliced_norm'] = unspliced_corrected - unspliced_corrected.min()
        spliced_corrected = adata_manager.get_from_registry('spliced')/samples["detection_y_cs"][0,:,:] - np.dot(obs2sample, samples["s_g_gene_add"][:,:,1])
        adata.layers['spliced_norm'] = spliced_corrected - spliced_corrected.min()

        return adata

    def export_posterior(
        self,
        adata,
        sample_kwargs: Optional[dict] = None,
        export_slot: str = "mod",
        full_velocity_posterior = False,
        normalize = True):
        """
        Summarises posterior distribution and exports results to anndata object. 
        Also computes RNAvelocity (based on posterior of rates)
        and normalized counts (based on posterior of technical variables)
        1. adata.obs: Latent time, sequencing depth constant
        2. adata.var: transcription/splicing/degredation rates, switch on and off times
        3. adata.uns: Posterior of all parameters ('mean', 'sd', 'q05', 'q95' and optionally all samples),
        model name, date
        4. adata.layers: 'velocity' (expected gradient of spliced counts), 'velocity_sd' (uncertainty in this gradient),
        'spliced_norm', 'unspliced_norm' (normalized counts)
        5. adata.uns: If "return_samples: True" and "full_velocity_posterior = True" full posterior distribution for velocity
        is saved in "adata.uns['velocity_posterior']". 
        Parameters
        ----------
        adata
            anndata object where results should be saved
        sample_kwargs
            optinoally a dictionary of arguments for self.sample_posterior, namely:
                num_samples - number of samples to use (Default = 1000).
                batch_size - data batch size (keep low enough to fit on GPU, default 2048).
                use_gpu - use gpu for generating samples?
                return_samples - export all posterior samples? (Otherwise just summary statistics)
        export_slot
            adata.uns slot where to export results
        full_velocity_posterior
            whether to save full posterior of velocity (only possible if "return_samples: True")
        normalize
            whether to compute normalized spliced and unspliced counts based on posterior of technical variables
        Returns
        -------
        adata with posterior added in adata.obs, adata.var and adata.uns
        """

        sample_kwargs = sample_kwargs if isinstance(sample_kwargs, dict) else dict()

        # generate samples from posterior distributions for all parameters
        # and compute mean, 5%/95% quantiles and standard deviation
        self.samples = self.sample_posterior(**sample_kwargs)

        # export posterior distribution summary for all parameters and
        # annotation (model, date, var, obs and cell type names) to anndata object
        adata.uns[export_slot] = self._export2adata(self.samples)

        if sample_kwargs['return_samples']:
            print('Warning: Saving ALL posterior samples. Specify "return_samples: False" to save just summary statistics.')
            adata.uns[export_slot]['post_samples'] = self.samples['posterior_samples']

        adata.obs['latent_time_mean'] = self.samples['post_sample_means']['T_c'].flatten()
        adata.obs['latent_time_sd'] = self.samples['post_sample_stds']['T_c'].flatten()
        adata.obs['normalization_factor_unspliced_mean'] = self.samples['post_sample_means']['detection_y_cu'].flatten()
        adata.obs['normalization_factor_unspliced_sd'] = self.samples['post_sample_stds']['detection_y_cu'].flatten()
        adata.obs['normalization_factor_spliced_mean'] = self.samples['post_sample_means']['detection_y_cs'].flatten()
        adata.obs['normalization_factor_spliced_sd'] = self.samples['post_sample_stds']['detection_y_cs'].flatten()

        adata.var['transcription_rate_mean'] = self.samples['post_sample_means']['alpha_g'].flatten()
        adata.var['transcription_rate_sd'] = self.samples['post_sample_stds']['alpha_g'].flatten()
        adata.var['splicing_rate_mean'] = self.samples['post_sample_means']['beta_g'].flatten()
        adata.var['splicing_rate_sd'] = self.samples['post_sample_stds']['beta_g'].flatten()
        adata.var['degredation_rate_mean'] = self.samples['post_sample_means']['gamma_g'].flatten()
        adata.var['degredation_rate_sd'] = self.samples['post_sample_stds']['gamma_g'].flatten()
        adata.var['switchON_time_mean'] = self.samples['post_sample_means']['T_gON'].flatten()
        adata.var['switchON_time_sd'] = self.samples['post_sample_stds']['T_gON'].flatten()
        adata.var['switchOFF_time_mean'] = self.samples['post_sample_means']['T_gOFF'].flatten()
        adata.var['switchOFF_time_sd'] = self.samples['post_sample_stds']['T_gOFF'].flatten()

        adata.layers['velocity'] = self.samples['post_sample_means']['beta_g'][...,0] * \
        self.samples['post_sample_means']['mu_RNAvelocity'][:,:,0] - \
        self.samples['post_sample_means']['gamma_g'][...,0] * self.samples['post_sample_means']['mu_RNAvelocity'][:,:,1]
        adata.layers['velocity_sd'] = np.sqrt((self.samples['post_sample_means']['beta_g'][...,0]**2 +
                                               self.samples['post_sample_stds']['beta_g'][...,0]**2)*
                                             (self.samples['post_sample_means']['mu_RNAvelocity'][:,:,0]**2 +
                                              self.samples['post_sample_stds']['mu_RNAvelocity'][:,:,0]**2) -
                                             (self.samples['post_sample_means']['beta_g'][...,0]**2*
                                              self.samples['post_sample_means']['mu_RNAvelocity'][:,:,0]**2) +
                                             (self.samples['post_sample_means']['gamma_g'][...,0]**2 +
                                              self.samples['post_sample_stds']['gamma_g'][...,0]**2)*
                                             (self.samples['post_sample_means']['mu_RNAvelocity'][:,:,1]**2 +
                                              self.samples['post_sample_stds']['mu_RNAvelocity'][:,:,1]**2) -
                                             (self.samples['post_sample_means']['gamma_g'][...,0]**2*
                                              self.samples['post_sample_means']['mu_RNAvelocity'][:,:,1]**2))
        if sample_kwargs['return_samples'] and full_velocity_posterior == True:
            print('Warning: Saving ALL posterior samples for velocity in "adata.uns["velocity_posterior"]". \
            Specify "return_samples: False" or "full_velocity_posterior = False" to save just summary statistics.')
            adata.uns['velocity_posterior'] = self.samples['posterior_samples']['beta_g'][...,0] * \
            self.samples['posterior_samples']['mu_RNAvelocity'][...,0] - self.samples['posterior_samples']['gamma_g'][...,0] * \
            self.samples['posterior_samples']['mu_RNAvelocity'][...,1]
        
        if normalize:
            print('Computing normalized counts based on posterior of technical variables.')
            adata = self.normalize(self.samples['post_sample_means'], self.adata_manager, adata)

        return adata
    def view_history(self):
        """
        View training history over various training windows to assess convergence or spot potential training problems.
        """
        def generatePlots():
            yield
            self.plot_history()
            yield
            self.plot_history(int(np.round(self.max_epochs/8)))
            yield
            self.plot_history(int(np.round(self.max_epochs/4)))
            yield
            self.plot_history(int(np.round(self.max_epochs/2)))
        multiplot_from_generator(generatePlots(), 4)
        
    def plot_QC(
        self,
        summary_name: str = "means",
        use_n_obs: int = 1000
    ):
        """
        Show quality control plots:
        1. Reconstruction accuracy to assess if there are any issues with model training.
            The two plots should be roughly diagonal, strong deviations signal problems that need to be investigated.
            Plotting is slow because expected value of mRNA count needs to be computed from model parameters. Random
            observations are used to speed up computation.

        Parameters
        ----------
        summary_name
            posterior distribution summary to use ('means', 'stds', 'q05', 'q95')
        use_n_obs
            how many random observations to use
        Returns
        -------
        """

        if getattr(self, "samples", False) is False:
            raise RuntimeError("self.samples is missing, please run self.export_posterior() first")
        if use_n_obs is not None:
            ind_x = np.random.choice(self.adata.n_obs, np.min((use_n_obs, self.adata.n_obs)), replace=False)
        else:
            ind_x = None

        self.expected_nb_param = self.module.model.compute_expected(
            self.samples[f"post_sample_{summary_name}"], self.adata_manager, ind_x=ind_x
        )
        def generatePlots():
            yield
            u_data = self.adata_manager.get_from_registry('unspliced')[ind_x, :]
            if issparse(u_data):
                u_data = np.asarray(u_data.toarray())
            self.plot_posterior_mu_vs_data(self.expected_nb_param["mu"][:,:,0], u_data)
            plt.title('Reconstruction Accuracy (unspliced)')
            yield
            s_data = self.adata_manager.get_from_registry('spliced')[ind_x, :]
            if issparse(s_data):
                s_data = np.asarray(s_data.toarray())    
            self.plot_posterior_mu_vs_data(self.expected_nb_param["mu"][:,:,1], s_data)
            plt.title('Reconstruction Accuracy (spliced)')
        multiplot_from_generator(generatePlots(), 3)