import inspect
import itertools
import multiprocessing
import os
from copy import deepcopy
from time import sleep
from typing import Tuple, Union, List, Optional

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from acvl_utils.cropping_and_padding.padding import pad_nd_image
from batchgenerators.dataloading.multi_threaded_augmenter import MultiThreadedAugmenter
from batchgenerators.utilities.file_and_folder_operations import load_json, join, isfile, maybe_mkdir_p, isdir, subdirs, \
    save_json
from torch import nn
from torch._dynamo import OptimizedModule
from torch.nn.parallel import DistributedDataParallel
from tqdm import tqdm

import sys
sys.path.append('.')
import nnseq2seq
from nnseq2seq.inference.data_iterators import PreprocessAdapterFromNpy, preprocessing_iterator_fromfiles, \
    preprocessing_iterator_fromnpy
from nnseq2seq.inference.export_prediction import export_prediction_from_logits, \
    convert_predicted_logits_to_segmentation_with_correct_shape
from nnseq2seq.inference.sliding_window_prediction import compute_gaussian, \
    compute_steps_for_sliding_window
from nnseq2seq.postprocessing.sitk_process import histMatch, linearMatch
from nnseq2seq.training.loss.metrics import torch_PSNR, np_SSIM, torch_LPIPS
from nnseq2seq.utilities.file_path_utilities import get_output_folder, check_workers_alive_and_busy
from nnseq2seq.utilities.find_class_by_name import recursive_find_python_class
from nnseq2seq.utilities.helpers import empty_cache, dummy_context
from nnseq2seq.utilities.json_export import recursive_fix_for_json_export
from nnseq2seq.utilities.label_handling.label_handling import determine_num_input_channels
from nnseq2seq.utilities.plans_handling.plans_handler import PlansManager, ConfigurationManager
from nnseq2seq.utilities.utils import create_lists_from_splitted_dataset_folder


class nnSeq2SeqPredictor(object):
    def __init__(self,
                 tile_step_size: float = 0.5,
                 use_gaussian: bool = True,
                 use_mirroring: bool = True,
                 perform_everything_on_device: bool = True,
                 device: torch.device = torch.device('cuda'),
                 verbose: bool = False,
                 verbose_preprocessing: bool = False,
                 allow_tqdm: bool = True):
        self.verbose = verbose
        self.verbose_preprocessing = verbose_preprocessing
        self.allow_tqdm = allow_tqdm

        self.plans_manager, self.configuration_manager, self.list_of_parameters, self.network, self.dataset_json, \
        self.trainer_name, self.allowed_mirroring_axes, self.label_manager = None, None, None, None, None, None, None, None

        self.tile_step_size = tile_step_size
        self.use_gaussian = use_gaussian
        self.use_mirroring = use_mirroring
        if device.type == 'cuda':
            # device = torch.device(type='cuda', index=0)  # set the desired GPU with CUDA_VISIBLE_DEVICES!
            pass
        if device.type != 'cuda':
            print(f'perform_everything_on_device=True is only supported for cuda devices! Setting this to False')
            perform_everything_on_device = False
        self.device = device
        self.perform_everything_on_device = perform_everything_on_device

    def initialize_from_trained_model_folder(self, model_training_output_dir: str,
                                             use_folds: Union[Tuple[Union[int, str]], None],
                                             checkpoint_name: str = 'checkpoint_final.pth'):
        """
        This is used when making predictions with a trained model
        """
        if use_folds is None:
            use_folds = nnSeq2SeqPredictor.auto_detect_available_folds(model_training_output_dir, checkpoint_name)

        dataset_json = load_json(join(model_training_output_dir, 'dataset.json'))
        plans = load_json(join(model_training_output_dir, 'plans.json'))
        plans_manager = PlansManager(plans)

        if isinstance(use_folds, str):
            use_folds = [use_folds]

        parameters = []
        for i, f in enumerate(use_folds):
            f = int(f) if f != 'all' else f
            checkpoint = torch.load(join(model_training_output_dir, f'fold_{f}', checkpoint_name),
                                    map_location=torch.device('cpu'))
            if i == 0:
                trainer_name = checkpoint['trainer_name']
                configuration_name = checkpoint['init_args']['configuration']
                inference_allowed_mirroring_axes = checkpoint['inference_allowed_mirroring_axes'] if \
                    'inference_allowed_mirroring_axes' in checkpoint.keys() else None

            parameters.append(checkpoint['network_weights'])

        configuration_manager = plans_manager.get_configuration(configuration_name)
        # restore network
        num_input_channels = determine_num_input_channels(plans_manager, configuration_manager, dataset_json)
        trainer_class = recursive_find_python_class(join(nnseq2seq.__path__[0], "training", "nnSeq2SeqTrainer"),
                                                    trainer_name, 'nnseq2seq.training.nnSeq2SeqTrainer')

        network = trainer_class.build_network_architecture(
            configuration_manager.network_arch_class_name,
            configuration_manager.network_arch_init_kwargs,
            configuration_manager.network_arch_init_kwargs_req_import,
            num_input_channels,
            plans_manager.get_label_manager(dataset_json).num_segmentation_heads,
            enable_deep_supervision=False
        )

        self.plans_manager = plans_manager
        self.configuration_manager = configuration_manager
        self.list_of_parameters = parameters
        self.network = network
        self.dataset_json = dataset_json
        self.trainer_name = trainer_name
        self.allowed_mirroring_axes = inference_allowed_mirroring_axes
        self.label_manager = plans_manager.get_label_manager(dataset_json)
        if ('nnSeq2Seq_compile' in os.environ.keys()) and (os.environ['nnSeq2Seq_compile'].lower() in ('true', '1', 't')) \
                and not isinstance(self.network, OptimizedModule):
            print('Using torch.compile')
            self.network = torch.compile(self.network)

    def manual_initialization(self, network: nn.Module, plans_manager: PlansManager,
                              configuration_manager: ConfigurationManager, parameters: Optional[List[dict]],
                              dataset_json: dict, trainer_name: str,
                              inference_allowed_mirroring_axes: Optional[Tuple[int, ...]]):
        """
        This is used by the nnSeq2SeqTrainer to initialize nnSeq2SeqPredictor for the final validation
        """
        self.plans_manager = plans_manager
        self.configuration_manager = configuration_manager
        self.list_of_parameters = parameters
        self.network = network
        self.dataset_json = dataset_json
        self.trainer_name = trainer_name
        self.allowed_mirroring_axes = inference_allowed_mirroring_axes
        self.label_manager = plans_manager.get_label_manager(dataset_json)
        allow_compile = True
        allow_compile = allow_compile and ('nnSeq2Seq_compile' in os.environ.keys()) and (
                    os.environ['nnSeq2Seq_compile'].lower() in ('true', '1', 't'))
        allow_compile = allow_compile and not isinstance(self.network, OptimizedModule)
        if isinstance(self.network, DistributedDataParallel):
            allow_compile = allow_compile and isinstance(self.network.module, OptimizedModule)
        if allow_compile:
            print('Using torch.compile')
            self.network = torch.compile(self.network)

    @staticmethod
    def auto_detect_available_folds(model_training_output_dir, checkpoint_name):
        print('use_folds is None, attempting to auto detect available folds')
        fold_folders = subdirs(model_training_output_dir, prefix='fold_', join=False)
        fold_folders = [i for i in fold_folders if i != 'fold_all']
        fold_folders = [i for i in fold_folders if isfile(join(model_training_output_dir, i, checkpoint_name))]
        use_folds = [int(i.split('_')[-1]) for i in fold_folders]
        print(f'found the following folds: {use_folds}')
        return use_folds

    def _manage_input_and_output_lists(self, list_of_lists_or_source_folder: Union[str, List[List[str]]],
                                       output_folder_or_list_of_truncated_output_files: Union[None, str, List[str]],
                                       folder_with_segs_from_prev_stage: str = None,
                                       overwrite: bool = True,
                                       part_id: int = 0,
                                       num_parts: int = 1,
                                       save_probabilities: bool = False):
        if isinstance(list_of_lists_or_source_folder, str):
            list_of_lists_or_source_folder = create_lists_from_splitted_dataset_folder(list_of_lists_or_source_folder,
                                                                                       self.dataset_json['file_ending'])
        print(f'There are {len(list_of_lists_or_source_folder)} cases in the source folder')
        list_of_lists_or_source_folder = list_of_lists_or_source_folder[part_id::num_parts]
        caseids = [os.path.basename(i[0])[:-(len(self.dataset_json['file_ending']) + 5)] for i in
                   list_of_lists_or_source_folder]
        print(
            f'I am process {part_id} out of {num_parts} (max process ID is {num_parts - 1}, we start counting with 0!)')
        print(f'There are {len(caseids)} cases that I would like to predict')

        if isinstance(output_folder_or_list_of_truncated_output_files, str):
            output_filename_truncated = [join(output_folder_or_list_of_truncated_output_files, i) for i in caseids]
        else:
            output_filename_truncated = output_folder_or_list_of_truncated_output_files

        seg_from_prev_stage_files = [join(folder_with_segs_from_prev_stage, i + self.dataset_json['file_ending']) if
                                     folder_with_segs_from_prev_stage is not None else None for i in caseids]
        # remove already predicted files form the lists
        if not overwrite and output_filename_truncated is not None:
            tmp = [isfile(i + self.dataset_json['file_ending']) for i in output_filename_truncated]
            if save_probabilities:
                tmp2 = [isfile(i + '.npz') for i in output_filename_truncated]
                tmp = [i and j for i, j in zip(tmp, tmp2)]
            not_existing_indices = [i for i, j in enumerate(tmp) if not j]

            output_filename_truncated = [output_filename_truncated[i] for i in not_existing_indices]
            list_of_lists_or_source_folder = [list_of_lists_or_source_folder[i] for i in not_existing_indices]
            seg_from_prev_stage_files = [seg_from_prev_stage_files[i] for i in not_existing_indices]
            print(f'overwrite was set to {overwrite}, so I am only working on cases that haven\'t been predicted yet. '
                  f'That\'s {len(not_existing_indices)} cases.')
        return list_of_lists_or_source_folder, output_filename_truncated, seg_from_prev_stage_files

    def predict_from_files(self,
                           list_of_lists_or_source_folder: Union[str, List[List[str]]],
                           output_folder_or_list_of_truncated_output_files: Union[str, None, List[str]],
                           save_probabilities: bool = False,
                           overwrite: bool = True,
                           num_processes_preprocessing: int = 8,
                           num_processes_segmentation_export: int = 8,
                           folder_with_segs_from_prev_stage: str = None,
                           num_parts: int = 1,
                           part_id: int = 0):
        """
        This is nnSeq2Seq's default function for making predictions. It works best for batch predictions
        (predicting many images at once).
        """
        if isinstance(output_folder_or_list_of_truncated_output_files, str):
            output_folder = output_folder_or_list_of_truncated_output_files
        elif isinstance(output_folder_or_list_of_truncated_output_files, list):
            output_folder = os.path.dirname(output_folder_or_list_of_truncated_output_files[0])
        else:
            output_folder = None

        ########################
        # let's store the input arguments so that its clear what was used to generate the prediction
        if output_folder is not None:
            my_init_kwargs = {}
            for k in inspect.signature(self.predict_from_files).parameters.keys():
                my_init_kwargs[k] = locals()[k]
            my_init_kwargs = deepcopy(
                my_init_kwargs)  # let's not unintentionally change anything in-place. Take this as a
            recursive_fix_for_json_export(my_init_kwargs)
            maybe_mkdir_p(output_folder)
            save_json(my_init_kwargs, join(output_folder, 'predict_from_raw_data_args.json'))

            # we need these two if we want to do things with the predictions like for example apply postprocessing
            save_json(self.dataset_json, join(output_folder, 'dataset.json'), sort_keys=False)
            save_json(self.plans_manager.plans, join(output_folder, 'plans.json'), sort_keys=False)
            # save task-specific contribution
            self.one2one_translate_psnr = [[[] for _ in range(self.network.tsf.num_channel)] for _ in range(self.network.tsf.num_channel)]
            ts_w = []
            for tgt_id in range(self.network.tsf.num_channel):
                tgt_code = torch.from_numpy(np.array([1 if i!=tgt_id else 0 for i in range(self.network.tsf.num_channel)] + [1 if i==tgt_id else 0 for i in range(self.network.tsf.num_channel)]+[0])).unsqueeze(0).to(dtype=torch.float32)
                w1 = self.network.tsf.infer_contribution(tgt_code)
                ts_w.append([tgt_id]+w1.squeeze().detach().numpy().tolist())
            seg_code = torch.from_numpy(np.array([1 for i in range(self.network.tsf.num_channel)] + [0 for i in range(self.network.tsf.num_channel)]+[1])).unsqueeze(0).to(dtype=torch.float32)
            w2 = self.network.tsf.infer_contribution(seg_code)
            ts_w.append(['seg']+w2.squeeze().detach().numpy().tolist())
            df = pd.DataFrame(data=ts_w, columns=['tgt']+['channel_{}'.format(i) for i in range(self.network.tsf.num_channel)])
            df.to_csv(join(output_folder, 'task-specific_sequence_contribution.csv'))
        #######################

        # check if we need a prediction from the previous stage
        if self.configuration_manager.previous_stage_name is not None:
            assert folder_with_segs_from_prev_stage is not None, \
                f'The requested configuration is a cascaded network. It requires the segmentations of the previous ' \
                f'stage ({self.configuration_manager.previous_stage_name}) as input. Please provide the folder where' \
                f' they are located via folder_with_segs_from_prev_stage'

        # sort out input and output filenames
        list_of_lists_or_source_folder, output_filename_truncated, seg_from_prev_stage_files = \
            self._manage_input_and_output_lists(list_of_lists_or_source_folder,
                                                output_folder_or_list_of_truncated_output_files,
                                                folder_with_segs_from_prev_stage, overwrite, part_id, num_parts,
                                                save_probabilities)
        if len(list_of_lists_or_source_folder) == 0:
            return

        data_iterator = self._internal_get_data_iterator_from_lists_of_filenames(list_of_lists_or_source_folder,
                                                                                 seg_from_prev_stage_files,
                                                                                 output_filename_truncated,
                                                                                 num_processes_preprocessing)

        return self.predict_from_data_iterator(data_iterator, save_probabilities, num_processes_segmentation_export)

    def _internal_get_data_iterator_from_lists_of_filenames(self,
                                                            input_list_of_lists: List[List[str]],
                                                            seg_from_prev_stage_files: Union[List[str], None],
                                                            output_filenames_truncated: Union[List[str], None],
                                                            num_processes: int):
        return preprocessing_iterator_fromfiles(input_list_of_lists, seg_from_prev_stage_files,
                                                output_filenames_truncated, self.plans_manager, self.dataset_json,
                                                self.configuration_manager, num_processes, self.device.type == 'cuda',
                                                self.verbose_preprocessing)
        # preprocessor = self.configuration_manager.preprocessor_class(verbose=self.verbose_preprocessing)
        # # hijack batchgenerators, yo
        # # we use the multiprocessing of the batchgenerators dataloader to handle all the background worker stuff. This
        # # way we don't have to reinvent the wheel here.
        # num_processes = max(1, min(num_processes, len(input_list_of_lists)))
        # ppa = PreprocessAdapter(input_list_of_lists, seg_from_prev_stage_files, preprocessor,
        #                         output_filenames_truncated, self.plans_manager, self.dataset_json,
        #                         self.configuration_manager, num_processes)
        # if num_processes == 0:
        #     mta = SingleThreadedAugmenter(ppa, None)
        # else:
        #     mta = MultiThreadedAugmenter(ppa, None, num_processes, 1, None, pin_memory=pin_memory)
        # return mta

    def get_data_iterator_from_raw_npy_data(self,
                                            image_or_list_of_images: Union[np.ndarray, List[np.ndarray]],
                                            segs_from_prev_stage_or_list_of_segs_from_prev_stage: Union[None,
                                                                                                        np.ndarray,
                                                                                                        List[
                                                                                                            np.ndarray]],
                                            properties_or_list_of_properties: Union[dict, List[dict]],
                                            truncated_ofname: Union[str, List[str], None],
                                            num_processes: int = 3):

        list_of_images = [image_or_list_of_images] if not isinstance(image_or_list_of_images, list) else \
            image_or_list_of_images

        if isinstance(segs_from_prev_stage_or_list_of_segs_from_prev_stage, np.ndarray):
            segs_from_prev_stage_or_list_of_segs_from_prev_stage = [
                segs_from_prev_stage_or_list_of_segs_from_prev_stage]

        if isinstance(truncated_ofname, str):
            truncated_ofname = [truncated_ofname]

        if isinstance(properties_or_list_of_properties, dict):
            properties_or_list_of_properties = [properties_or_list_of_properties]

        num_processes = min(num_processes, len(list_of_images))
        pp = preprocessing_iterator_fromnpy(
            list_of_images,
            segs_from_prev_stage_or_list_of_segs_from_prev_stage,
            properties_or_list_of_properties,
            truncated_ofname,
            self.plans_manager,
            self.dataset_json,
            self.configuration_manager,
            num_processes,
            self.device.type == 'cuda',
            self.verbose_preprocessing
        )

        return pp

    def predict_from_list_of_npy_arrays(self,
                                        image_or_list_of_images: Union[np.ndarray, List[np.ndarray]],
                                        segs_from_prev_stage_or_list_of_segs_from_prev_stage: Union[None,
                                                                                                    np.ndarray,
                                                                                                    List[
                                                                                                        np.ndarray]],
                                        properties_or_list_of_properties: Union[dict, List[dict]],
                                        truncated_ofname: Union[str, List[str], None],
                                        num_processes: int = 3,
                                        save_probabilities: bool = False,
                                        num_processes_segmentation_export: int = 8):
        iterator = self.get_data_iterator_from_raw_npy_data(image_or_list_of_images,
                                                            segs_from_prev_stage_or_list_of_segs_from_prev_stage,
                                                            properties_or_list_of_properties,
                                                            truncated_ofname,
                                                            num_processes)
        return self.predict_from_data_iterator(iterator, save_probabilities, num_processes_segmentation_export)

    def predict_from_data_iterator(self,
                                   data_iterator,
                                   save_probabilities: bool = False,
                                   num_processes_segmentation_export: int = 8):
        """
        each element returned by data_iterator must be a dict with 'data', 'ofile' and 'data_properties' keys!
        If 'ofile' is None, the result will be returned instead of written to a file
        """
        with multiprocessing.get_context("spawn").Pool(num_processes_segmentation_export) as export_pool:
            worker_list = [i for i in export_pool._pool]
            r = []
            for preprocessed in data_iterator:
                data = preprocessed['data']
                if isinstance(data, str):
                    delfile = data
                    data = torch.from_numpy(np.load(data))
                    os.remove(delfile)

                ofile = preprocessed['ofile']
                if ofile is not None:
                    print(f'\nPredicting {os.path.basename(ofile)}:')
                else:
                    print(f'\nPredicting image of shape {data.shape}:')

                print(f'perform_everything_on_device: {self.perform_everything_on_device}')

                properties = preprocessed['data_properties']

                # let's not get into a runaway situation where the GPU predicts so fast that the disk has to b swamped with
                # npy files
                proceed = not check_workers_alive_and_busy(export_pool, worker_list, r, allowed_num_queued=2)
                while not proceed:
                    sleep(0.1)
                    proceed = not check_workers_alive_and_busy(export_pool, worker_list, r, allowed_num_queued=2)
                
                for tgt_seq in range(properties['num_channel']):
                    tgt_code = F.one_hot(torch.from_numpy(np.array([tgt_seq], dtype=np.int64)),
                        num_classes=properties['num_channel']).to(self.device, dtype=data.dtype, non_blocking=True)
                    
                    tsf_prediction, _, _ = self.predict_logits_from_preprocessed_data(data, tgt_code, properties=properties, with_attn=False)
                    tsf_prediction_finetune, tsf_prediction_mask_finetune, _ = self.predict_logits_from_preprocessed_data(data, tgt_code, properties=properties, with_attn=True)
                    tsf_prediction_mask_finetune = torch.argmax(tsf_prediction_mask_finetune, dim=0, keepdim=True).cpu()
                    tsem = torch.abs(tsf_prediction_finetune-tsf_prediction)
                    os.makedirs(os.path.join(ofile, 'multi2one_inference'), exist_ok=True)
                    r.append(
                        export_pool.starmap_async(
                            export_prediction_from_logits,
                            ((tsf_prediction_finetune, properties, self.configuration_manager, self.plans_manager,
                            self.dataset_json, os.path.join(ofile, 'multi2one_inference', 'translate_tgt_{}'.format(tgt_seq)), save_probabilities),)
                        )
                    )
                    r.append(
                        export_pool.starmap_async(
                            export_prediction_from_logits,
                            ((tsf_prediction_mask_finetune, properties, self.configuration_manager, self.plans_manager,
                            self.dataset_json, os.path.join(ofile, 'multi2one_inference', 'segment'), save_probabilities),)
                        )
                    )
                    os.makedirs(os.path.join(ofile, 'explainability_visualization/task-specific_enhanced_map'), exist_ok=True)
                    r.append(
                        export_pool.starmap_async(
                            export_prediction_from_logits,
                            ((tsem, properties, self.configuration_manager, self.plans_manager,
                            self.dataset_json, os.path.join(ofile, 'explainability_visualization/task-specific_enhanced_map', 'task-specific_enhanced_map_tgt_{}'.format(tgt_seq)), save_probabilities),)
                        )
                    )

                    md = torch.zeros_like(data[0:1])
                    for src_idx, src_seq in enumerate(properties['available_channel']):
                        prediction, prediction_mask, prediction_latent = self.predict_logits_from_preprocessed_data(data[src_idx:src_idx+1], tgt_code)
                        prediction_mask = torch.argmax(prediction_mask, dim=0, keepdim=True).cpu()
                        prediction_latent = F.interpolate(prediction_latent.to(dtype=torch.float32).unsqueeze(0), scale_factor=0.25)[0].cpu()

                        if tgt_seq in properties['available_channel']:
                            tgt_idx = properties['available_channel'].index(tgt_seq)
                            hm_prediction = linearMatch(prediction, data[tgt_idx:tgt_idx+1])
                            #hm_prediction = histMatch(prediction[0], data[tgt_idx], is_torch=True).unsqueeze(0)
                            #hm_prediction = prediction
                            md += torch.abs(hm_prediction-data[tgt_idx:tgt_idx+1])
                            psnr = torch_PSNR(data[tgt_idx:tgt_idx+1], hm_prediction, data_range=1).item()
                            self.one2one_translate_psnr[src_seq][tgt_seq].append(psnr)

                        if ofile is not None:
                            # this needs to go into background processes
                            # export_prediction_from_logits(prediction, properties, self.configuration_manager, self.plans_manager,
                            #                               self.dataset_json, ofile, save_probabilities)
                            print('sending off prediction to background worker for resampling and export')
                            os.makedirs(os.path.join(ofile, 'normalized_source_images'), exist_ok=True)
                            if tgt_seq==0:
                                r.append(
                                    export_pool.starmap_async(
                                        export_prediction_from_logits,
                                        ((data[src_idx:src_idx+1], properties, self.configuration_manager, self.plans_manager,
                                        self.dataset_json, os.path.join(ofile, 'normalized_source_images', 'norm_src_{}'.format(src_seq)), save_probabilities),)
                                    )
                                )
                            
                            os.makedirs(os.path.join(ofile, 'one2one_inference'), exist_ok=True)
                            r.append(
                                export_pool.starmap_async(
                                    export_prediction_from_logits,
                                    ((prediction, properties, self.configuration_manager, self.plans_manager,
                                    self.dataset_json, os.path.join(ofile, 'one2one_inference', 'translate_src_{}_to_tgt_{}'.format(src_seq, tgt_seq)), save_probabilities),)
                                )
                            )
                            r.append(
                                export_pool.starmap_async(
                                    export_prediction_from_logits,
                                    ((prediction_mask, properties, self.configuration_manager, self.plans_manager,
                                    self.dataset_json, os.path.join(ofile, 'one2one_inference', 'segment_src_{}'.format(src_seq)), save_probabilities),)
                                )
                            )
                            os.makedirs(os.path.join(ofile, 'latent_space'), exist_ok=True)
                            r.append(
                                export_pool.starmap_async(
                                    export_prediction_from_logits,
                                    ((prediction_latent, properties, self.configuration_manager, self.plans_manager,
                                    self.dataset_json, os.path.join(ofile, 'latent_space', 'latent_space_src_{}'.format(src_seq)), save_probabilities, True),)
                                )
                            )

                            if src_idx==len(properties['available_channel'])-1:
                                os.makedirs(os.path.join(ofile, 'explainability_visualization/imaging_differentiation_map'), exist_ok=True)
                                
                                md /= len(properties['available_channel'])
                                
                                if tgt_seq in properties['available_channel']:
                                    r.append(
                                        export_pool.starmap_async(
                                            export_prediction_from_logits,
                                            ((md, properties, self.configuration_manager, self.plans_manager,
                                            self.dataset_json, os.path.join(ofile, 'explainability_visualization/imaging_differentiation_map', 'imaging_differentiation_map_tgt_{}'.format(tgt_seq)), save_probabilities),)
                                        )
                                    )
                        else:
                            # convert_predicted_logits_to_segmentation_with_correct_shape(
                            #             prediction, self.plans_manager,
                            #              self.configuration_manager, self.label_manager,
                            #              properties,
                            #              save_probabilities)

                            print('sending off prediction to background worker for resampling')
                            r.append(
                                export_pool.starmap_async(
                                    convert_predicted_logits_to_segmentation_with_correct_shape, (
                                        (prediction, self.plans_manager,
                                        self.configuration_manager, self.label_manager,
                                        properties,
                                        save_probabilities),)
                                )
                            )
                        if ofile is not None:
                            print(f'done with {os.path.basename(ofile)} from src {src_seq} to tgt {tgt_seq}')
                        else:
                            print(f'\nDone with image of shape {data.shape}:')
            ret = [i.get()[0] for i in r]

        if isinstance(data_iterator, MultiThreadedAugmenter):
            data_iterator._finish()
        
        # calculate synthesis-based sequence contribution
        sbsc = []
        N = self.network.tsf.num_channel
        A = [[0 for i in range(N)] for i in range(N)]
        eps = 1e-9
        all_psnr = []
        for p1 in self.one2one_translate_psnr:
            for p2 in p1:
                all_psnr += p2
        for i in range(N):
            for j in range(N):
                if len(self.one2one_translate_psnr[i][j])==0:
                    continue
                A[i][j] = (np.nanmean(self.one2one_translate_psnr[i][j])-np.nanmean(all_psnr))/(np.nanstd(all_psnr) + eps)
        for i in range(N):
            metric_ct = 0
            metric_cd = 0
            for j in range(N):
                metric_ct += A[i][j]
                metric_cd -= A[j][i]
            metric_ct /= N
            metric_cd /= N
            sbsc.append([i, metric_ct, metric_cd])
        df = pd.DataFrame(data=sbsc, columns=['channel', 'metric_ct', 'metric_cd'])
        df.to_csv(join(os.path.dirname(ofile), 'synthesis-based_sequence_contribution.csv'))

        # clear lru cache
        compute_gaussian.cache_clear()
        # clear device cache
        empty_cache(self.device)
        return ret

    def predict_single_npy_array(self, input_image: np.ndarray, image_properties: dict,
                                 segmentation_previous_stage: np.ndarray = None,
                                 output_file_truncated: str = None,
                                 save_or_return_probabilities: bool = False):
        """
        image_properties must only have a 'spacing' key!
        """
        ppa = PreprocessAdapterFromNpy([input_image], [segmentation_previous_stage], [image_properties],
                                       [output_file_truncated],
                                       self.plans_manager, self.dataset_json, self.configuration_manager,
                                       num_threads_in_multithreaded=1, verbose=self.verbose)
        if self.verbose:
            print('preprocessing')
        dct = next(ppa)

        if self.verbose:
            print('predicting')
        predicted_logits, predicted_mask_logits, predicted_latent_space = self.predict_logits_from_preprocessed_data(dct['data'])
        predicted_logits = predicted_logits.cpu()
        predicted_mask_logits = predicted_mask_logits.cpu()
        predicted_latent_space = predicted_latent_space.cpu()

        if self.verbose:
            print('resampling to original shape')
        if output_file_truncated is not None:
            export_prediction_from_logits(predicted_logits, dct['data_properties'], self.configuration_manager,
                                          self.plans_manager, self.dataset_json, output_file_truncated,
                                          save_or_return_probabilities)
        else:
            ret = convert_predicted_logits_to_segmentation_with_correct_shape(predicted_logits, self.plans_manager,
                                                                              self.configuration_manager,
                                                                              self.label_manager,
                                                                              dct['data_properties'],
                                                                              return_probabilities=
                                                                              save_or_return_probabilities)
            if save_or_return_probabilities:
                return ret[0], ret[1]
            else:
                return ret

    def predict_logits_from_preprocessed_data(self, data: torch.Tensor, target_code: torch.Tensor, properties=None, with_attn: bool=True) -> torch.Tensor:
        """
        IMPORTANT! IF YOU ARE RUNNING THE CASCADE, THE SEGMENTATION FROM THE PREVIOUS STAGE MUST ALREADY BE STACKED ON
        TOP OF THE IMAGE AS ONE-HOT REPRESENTATION! SEE PreprocessAdapter ON HOW THIS SHOULD BE DONE!

        RETURNED LOGITS HAVE THE SHAPE OF THE INPUT. THEY MUST BE CONVERTED BACK TO THE ORIGINAL IMAGE SIZE.
        SEE convert_predicted_logits_to_segmentation_with_correct_shape
        """
        n_threads = torch.get_num_threads()
        torch.set_num_threads(8 if 8 < n_threads else n_threads)
        with torch.no_grad():
            prediction = None
            prediction_mask = None
            prediction_latent = None

            for params in self.list_of_parameters:

                # messing with state dict names...
                if not isinstance(self.network, OptimizedModule):
                    self.network.load_state_dict(params)
                else:
                    self.network._orig_mod.load_state_dict(params)

                # why not leave prediction on device if perform_everything_on_device? Because this may cause the
                # second iteration to crash due to OOM. Grabbing that with try except cause way more bloated code than
                # this actually saves computation time
                if prediction is None:
                    prediction, prediction_mask, prediction_latent = self.predict_sliding_window_return_logits(data, target_code, properties, with_attn)
                    prediction = prediction.to('cpu')
                    prediction_mask = prediction_mask.to('cpu')
                    prediction_latent = prediction_latent.to('cpu')
                else:
                    pred, pred_mask, pred_latent = self.predict_sliding_window_return_logits(data, target_code, properties, with_attn)
                    prediction += pred.to('cpu')
                    prediction_mask += pred_mask.to('cpu')
                    prediction_latent += pred_latent.to('cpu')

            if len(self.list_of_parameters) > 1:
                prediction /= len(self.list_of_parameters)
                prediction_mask /= len(self.list_of_parameters)
                prediction_latent /= len(self.list_of_parameters)

            if self.verbose: print('Prediction done')
            prediction = prediction.to('cpu')
            prediction_mask = prediction_mask.to('cpu')
            prediction_latent = prediction_latent.to('cpu')
        torch.set_num_threads(n_threads)
        return prediction, prediction_mask, prediction_latent

    def _internal_get_sliding_window_slicers(self, image_size: Tuple[int, ...]):
        slicers = []
        if len(self.configuration_manager.patch_size) < len(image_size):
            assert len(self.configuration_manager.patch_size) == len(
                image_size) - 1, 'if tile_size has less entries than image_size, ' \
                                 'len(tile_size) ' \
                                 'must be one shorter than len(image_size) ' \
                                 '(only dimension ' \
                                 'discrepancy of 1 allowed).'
            steps = compute_steps_for_sliding_window(image_size[1:], self.configuration_manager.patch_size,
                                                     self.tile_step_size)
            if self.verbose: print(f'n_steps {image_size[0] * len(steps[0]) * len(steps[1])}, image size is'
                                   f' {image_size}, tile_size {self.configuration_manager.patch_size}, '
                                   f'tile_step_size {self.tile_step_size}\nsteps:\n{steps}')
            for d in range(image_size[0]):
                for sx in steps[0]:
                    for sy in steps[1]:
                        slicers.append(
                            tuple([slice(None), d, *[slice(si, si + ti) for si, ti in
                                                     zip((sx, sy), self.configuration_manager.patch_size)]]))
        else:
            steps = compute_steps_for_sliding_window(image_size, self.configuration_manager.patch_size,
                                                     self.tile_step_size)
            if self.verbose: print(
                f'n_steps {np.prod([len(i) for i in steps])}, image size is {image_size}, tile_size {self.configuration_manager.patch_size}, '
                f'tile_step_size {self.tile_step_size}\nsteps:\n{steps}')
            for sx in steps[0]:
                for sy in steps[1]:
                    for sz in steps[2]:
                        slicers.append(
                            tuple([slice(None), *[slice(si, si + ti) for si, ti in
                                                  zip((sx, sy, sz), self.configuration_manager.patch_size)]]))
        return slicers

    def _internal_maybe_mirror_and_predict(self, x: torch.Tensor, target_code: torch.Tensor, properties=None, with_attn: bool=True) -> torch.Tensor:
        mirror_axes = self.allowed_mirroring_axes if self.use_mirroring else None
        if properties is None:
            prediction, latent_space, _ = self.network(x, target_code, with_latent=True)
            prediction_mask = self.network.segmentor(latent_space)
        else:
            tsf_data = [torch.zeros_like(x[:,0:1]) for _ in range(properties['num_channel'])]
            for i, seq_i in enumerate(properties['available_channel']):
                tsf_data[seq_i] = x[:, i:i+1]
            tsf_data = torch.cat(tsf_data, dim=1)
            tsf_data = tsf_data.view(-1,1,*x.shape[2:]).to(self.device, non_blocking=True)
            latent_tsf, _ = self.network.image_encoder(tsf_data)
            latent_tsf = latent_tsf.reshape(x.shape[0], -1, *latent_tsf.shape[2:])
            
            tsf_src_code = torch.from_numpy(np.array([1 if i in properties['available_channel'] else 0 for i in range(properties['num_channel'])])).unsqueeze(0).to(self.device, non_blocking=True)
            tsf_tgt_code = torch.cat([tsf_src_code, target_code, torch.zeros((target_code.shape[0],1), device=self.device)], dim=1)
            tsf_seg_code = torch.cat([tsf_src_code, torch.zeros_like(target_code), torch.ones((target_code.shape[0],1), device=self.device)], dim=1)

            latent_tsf_tgt, latent_tsf_tgt_finetune = self.network.tsf(latent_tsf, tsf_tgt_code)
            latent_tsf_seg, latent_tsf_seg_finetune = self.network.tsf(latent_tsf, tsf_seg_code)
            if with_attn:
                prediction = self.network.hyper_decoder(latent_tsf_tgt_finetune, target_code)
                prediction_mask = self.network.segmentor(latent_tsf_seg_finetune)
                latent_space = latent_tsf_tgt_finetune
            else:
                prediction = self.network.hyper_decoder(latent_tsf_tgt, target_code)
                prediction_mask = self.network.segmentor(latent_tsf_seg)
                latent_space = latent_tsf_tgt
            

        if mirror_axes is not None:
            # check for invalid numbers in mirror_axes
            # x should be 5d for 3d images and 4d for 2d. so the max value of mirror_axes cannot exceed len(x.shape) - 3
            assert max(mirror_axes) <= x.ndim - 3, 'mirror_axes does not match the dimension of the input!'

            axes_combinations = [
                c for i in range(len(mirror_axes)) for c in itertools.combinations([m + 2 for m in mirror_axes], i + 1)
            ]
            for axes in axes_combinations:
                if properties is None:
                    pred, latent, _ = self.network(torch.flip(x, (*axes,)), target_code, with_latent=True)
                    pred_mask = self.network.segmentor(latent)
                else:
                    latent_tsf, _ = self.network.image_encoder(torch.flip(tsf_data, (*axes,)))
                    latent_tsf = latent_tsf.reshape(x.shape[0], -1, *latent_tsf.shape[2:])
                    
                    latent_tsf_tgt, latent_tsf_tgt_finetune = self.network.tsf(latent_tsf, tsf_tgt_code)
                    latent_tsf_seg, latent_tsf_seg_finetune = self.network.tsf(latent_tsf, tsf_seg_code)
                    if with_attn:
                        pred = self.network.hyper_decoder(latent_tsf_tgt_finetune, target_code)
                        pred_mask = self.network.segmentor(latent_tsf_seg_finetune)
                        latent = latent_tsf_tgt_finetune
                    else:
                        pred = self.network.hyper_decoder(latent_tsf_tgt, target_code)
                        pred_mask = self.network.segmentor(latent_tsf_seg)
                        latent = latent_tsf_tgt
                prediction += torch.flip(pred, (*axes,))
                prediction_mask += torch.flip(pred_mask, (*axes,))
                latent_space += torch.flip(latent, (*axes,))
            prediction /= (len(axes_combinations) + 1)
            prediction_mask /= (len(axes_combinations) + 1)
            latent_space /= (len(axes_combinations) + 1)
        return prediction, prediction_mask, latent_space

    def _internal_predict_sliding_window_return_logits(self,
                                                       data: torch.Tensor,
                                                       slicers,
                                                       target_code: torch.Tensor,
                                                       do_on_device: bool = True,
                                                       properties=None, with_attn: bool=True
                                                       ):
        predicted_logits = n_predictions = prediction = gaussian = workon = None
        results_device = self.device if do_on_device else torch.device('cpu')

        try:
            empty_cache(self.device)

            # move data to device
            if self.verbose:
                print(f'move image to device {results_device}')
            data = data.to(results_device)

            # preallocate arrays
            if self.verbose:
                print(f'preallocating results arrays on device {results_device}')
            predicted_logits = torch.zeros((1, *data.shape[1:]),
                                           dtype=torch.half,
                                           device=results_device)
            predicted_mask_logits = torch.zeros((self.label_manager.num_segmentation_heads, *data.shape[1:]),
                                           dtype=torch.half,
                                           device=results_device)
            predicted_latent_space = torch.zeros((self.network.image_encoder.latent_space_dim, *data.shape[1:]),
                                           dtype=torch.half,
                                           device=results_device)
            n_predictions = torch.zeros(data.shape[1:], dtype=torch.half, device=results_device)
            if self.use_gaussian:
                gaussian = compute_gaussian(tuple(self.configuration_manager.patch_size), sigma_scale=1. / 8,
                                            value_scaling_factor=10,
                                            device=results_device)

            if self.verbose: print('running prediction')
            if not self.allow_tqdm and self.verbose: print(f'{len(slicers)} steps')
            for sl in tqdm(slicers, disable=not self.allow_tqdm):
                workon = data[sl][None]
                workon = workon.to(self.device, non_blocking=False)

                prediction, prediction_mask, latent_space = self._internal_maybe_mirror_and_predict(workon, target_code, properties, with_attn)
                prediction = prediction[0].to(results_device)
                prediction_mask = prediction_mask[0].to(results_device)
                latent_space = F.interpolate(latent_space, scale_factor=4)[0].to(results_device)

                predicted_logits[sl] += (prediction * gaussian if self.use_gaussian else prediction)
                predicted_mask_logits[sl] += (prediction_mask * gaussian if self.use_gaussian else prediction_mask)
                predicted_latent_space[sl] += (latent_space * gaussian if self.use_gaussian else latent_space)
                n_predictions[sl[1:]] += (gaussian if self.use_gaussian else 1)
                

            predicted_logits /= n_predictions
            predicted_mask_logits /= n_predictions
            predicted_latent_space /= n_predictions
            # check for infs
            if torch.any(torch.isinf(predicted_logits)):
                raise RuntimeError('Encountered inf in predicted array. Aborting... If this problem persists, '
                                   'reduce value_scaling_factor in compute_gaussian or increase the dtype of '
                                   'predicted_logits to fp32')
        except Exception as e:
            del predicted_logits, n_predictions, prediction, prediction_mask, latent_space, gaussian, workon
            empty_cache(self.device)
            empty_cache(results_device)
            raise e
        return predicted_logits, predicted_mask_logits, predicted_latent_space

    def predict_sliding_window_return_logits(self, input_image: torch.Tensor, target_code: torch.Tensor, properties=None, with_attn: bool=True) \
            -> Union[np.ndarray, torch.Tensor]:
        assert isinstance(input_image, torch.Tensor)
        self.network = self.network.to(self.device)
        self.network.eval()

        empty_cache(self.device)

        # Autocast can be annoying
        # If the device_type is 'cpu' then it's slow as heck on some CPUs (no auto bfloat16 support detection)
        # and needs to be disabled.
        # If the device_type is 'mps' then it will complain that mps is not implemented, even if enabled=False
        # is set. Whyyyyyyy. (this is why we don't make use of enabled=False)
        # So autocast will only be active if we have a cuda device.
        with torch.no_grad():
            with torch.autocast(self.device.type, enabled=True) if self.device.type == 'cuda' else dummy_context():
                assert input_image.ndim == 4, 'input_image must be a 4D np.ndarray or torch.Tensor (c, x, y, z)'

                if self.verbose: print(f'Input shape: {input_image.shape}')
                if self.verbose: print("step_size:", self.tile_step_size)
                if self.verbose: print("mirror_axes:", self.allowed_mirroring_axes if self.use_mirroring else None)

                # if input_image is smaller than tile_size we need to pad it to tile_size.
                data, slicer_revert_padding = pad_nd_image(input_image, self.configuration_manager.patch_size,
                                                           'constant', {'value': 0}, True,
                                                           None)

                slicers = self._internal_get_sliding_window_slicers(data.shape[1:])

                if self.perform_everything_on_device and self.device != 'cpu':
                    # we need to try except here because we can run OOM in which case we need to fall back to CPU as a results device
                    try:
                        predicted_logits, predicted_mask_logits, predicted_latent_space = self._internal_predict_sliding_window_return_logits(data, slicers, target_code,
                                                                                               self.perform_everything_on_device, properties, with_attn)
                    except RuntimeError:
                        print(
                            'Prediction on device was unsuccessful, probably due to a lack of memory. Moving results arrays to CPU')
                        empty_cache(self.device)
                        predicted_logits, predicted_mask_logits, predicted_latent_space = self._internal_predict_sliding_window_return_logits(data, slicers, target_code, False, properties, with_attn)
                else:
                    predicted_logits, predicted_mask_logits, predicted_latent_space = self._internal_predict_sliding_window_return_logits(data, slicers, target_code,
                                                                                           self.perform_everything_on_device, properties, with_attn)

                empty_cache(self.device)
                # revert padding
                predicted_logits = predicted_logits[tuple([slice(None), *slicer_revert_padding[1:]])]
                predicted_mask_logits = predicted_mask_logits[tuple([slice(None), *slicer_revert_padding[1:]])]
                predicted_latent_space = predicted_latent_space[tuple([slice(None), *slicer_revert_padding[1:]])]
        return predicted_logits, predicted_mask_logits, predicted_latent_space


def predict_entry_point_modelfolder():
    import argparse
    parser = argparse.ArgumentParser(description='Use this to run inference with nnSeq2Seq. This function is used when '
                                                 'you want to manually specify a folder containing a trained nnSeq2Seq '
                                                 'model. This is useful when the nnseq2seq environment variables '
                                                 '(nnSeq2Seq_results) are not set.')
    parser.add_argument('-i', type=str, required=True,
                        help='input folder. Remember to use the correct channel numberings for your files (_0000 etc). '
                             'File endings must be the same as the training dataset!')
    parser.add_argument('-o', type=str, required=True,
                        help='Output folder. If it does not exist it will be created. Predicted segmentations will '
                             'have the same name as their source images.')
    parser.add_argument('-m', type=str, required=True,
                        help='Folder in which the trained model is. Must have subfolders fold_X for the different '
                             'folds you trained')
    parser.add_argument('-f', nargs='+', type=str, required=False, default=(0, 1, 2, 3, 4),
                        help='Specify the folds of the trained model that should be used for prediction. '
                             'Default: (0, 1, 2, 3, 4)')
    parser.add_argument('-step_size', type=float, required=False, default=0.5,
                        help='Step size for sliding window prediction. The larger it is the faster but less accurate '
                             'the prediction. Default: 0.5. Cannot be larger than 1. We recommend the default.')
    parser.add_argument('--disable_tta', action='store_true', required=False, default=False,
                        help='Set this flag to disable test time data augmentation in the form of mirroring. Faster, '
                             'but less accurate inference. Not recommended.')
    parser.add_argument('--verbose', action='store_true', help="Set this if you like being talked to. You will have "
                                                               "to be a good listener/reader.")
    parser.add_argument('--save_probabilities', action='store_true',
                        help='Set this to export predicted class "probabilities". Required if you want to ensemble '
                             'multiple configurations.')
    parser.add_argument('--continue_prediction', '--c', action='store_true',
                        help='Continue an aborted previous prediction (will not overwrite existing files)')
    parser.add_argument('-chk', type=str, required=False, default='checkpoint_final.pth',
                        help='Name of the checkpoint you want to use. Default: checkpoint_final.pth')
    parser.add_argument('-npp', type=int, required=False, default=3,
                        help='Number of processes used for preprocessing. More is not always better. Beware of '
                             'out-of-RAM issues. Default: 3')
    parser.add_argument('-nps', type=int, required=False, default=3,
                        help='Number of processes used for segmentation export. More is not always better. Beware of '
                             'out-of-RAM issues. Default: 3')
    parser.add_argument('-prev_stage_predictions', type=str, required=False, default=None,
                        help='Folder containing the predictions of the previous stage. Required for cascaded models.')
    parser.add_argument('-device', type=str, default='cuda', required=False,
                        help="Use this to set the device the inference should run with. Available options are 'cuda' "
                             "(GPU), 'cpu' (CPU) and 'mps' (Apple M1/M2). Do NOT use this to set which GPU ID! "
                             "Use CUDA_VISIBLE_DEVICES=X nnSeq2Seqv2_predict [...] instead!")
    parser.add_argument('--disable_progress_bar', action='store_true', required=False, default=False,
                        help='Set this flag to disable progress bar. Recommended for HPC environments (non interactive '
                             'jobs)')

    print(
        "\n#######################################################################\nPlease cite the following paper "
        "when using nnSeq2Seq:\n"
        "[1] Han L, Tan T, Zhang T, et al. "
        "Synthesis-based imaging-differentiation representation learning for multi-sequence 3D/4D MRI[J]. "
        "Medical Image Analysis, 2024, 92: 103044.\n"
        "[2] Han L, Zhang T, Huang Y, et al. "
        "An Explainable Deep Framework: Towards Task-Specific Fusion for Multi-to-One MRI Synthesis[C]. "
        "International Conference on Medical Image Computing and Computer-Assisted Intervention. Cham: Springer Nature Switzerland, 2023: 45-55.\n"
        "#######################################################################\n",)

    args = parser.parse_args()
    args.f = [i if i == 'all' else int(i) for i in args.f]

    if not isdir(args.o):
        maybe_mkdir_p(args.o)

    assert args.device in ['cpu', 'cuda',
                           'mps'], f'-device must be either cpu, mps or cuda. Other devices are not tested/supported. Got: {args.device}.'
    if args.device == 'cpu':
        # let's allow torch to use hella threads
        import multiprocessing
        torch.set_num_threads(multiprocessing.cpu_count())
        device = torch.device('cpu')
    elif args.device == 'cuda':
        # multithreading in torch doesn't help nnSeq2Seq if run on GPU
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        device = torch.device('cuda')
    else:
        device = torch.device('mps')

    predictor = nnSeq2SeqPredictor(tile_step_size=args.step_size,
                                use_gaussian=True,
                                use_mirroring=not args.disable_tta,
                                perform_everything_on_device=True,
                                device=device,
                                verbose=args.verbose,
                                allow_tqdm=not args.disable_progress_bar,
                                verbose_preprocessing=args.verbose)
    predictor.initialize_from_trained_model_folder(args.m, args.f, args.chk)
    predictor.predict_from_files(args.i, args.o, save_probabilities=args.save_probabilities,
                                 overwrite=not args.continue_prediction,
                                 num_processes_preprocessing=args.npp,
                                 num_processes_segmentation_export=args.nps,
                                 folder_with_segs_from_prev_stage=args.prev_stage_predictions,
                                 num_parts=1, part_id=0)


def predict_entry_point():
    import argparse
    parser = argparse.ArgumentParser(description='Use this to run inference with nnSeq2Seq. This function is used when '
                                                 'you want to manually specify a folder containing a trained nnSeq2Seq '
                                                 'model. This is useful when the nnseq2seq environment variables '
                                                 '(nnSeq2Seq_results) are not set.')
    parser.add_argument('-i', type=str, required=True,
                        help='input folder. Remember to use the correct channel numberings for your files (_0000 etc). '
                             'File endings must be the same as the training dataset!')
    parser.add_argument('-o', type=str, required=True,
                        help='Output folder. If it does not exist it will be created.')
    parser.add_argument('-d', type=str, required=True,
                        help='Dataset with which you would like to predict. You can specify either dataset name or id')
    parser.add_argument('-c', type=str, required=True,
                        help='nnSeq2Seq configuration that should be used for prediction. Config must be located '
                             'in the plans specified with -p')
    parser.add_argument('-p', type=str, required=False, default='nnSeq2SeqPlans',
                        help='Plans identifier. Specify the plans in which the desired configuration is located. '
                             'Default: nnSeq2SeqPlans')
    parser.add_argument('-tr', type=str, required=False, default='nnSeq2SeqTrainer',
                        help='What nnSeq2Seq trainer class was used for training? Default: nnSeq2SeqTrainer')
    parser.add_argument('-f', nargs='+', type=str, required=False, default=(0, 1, 2, 3, 4),
                        help='Specify the folds of the trained model that should be used for prediction. '
                             'Default: (0, 1, 2, 3, 4)')
    parser.add_argument('-step_size', type=float, required=False, default=0.5,
                        help='Step size for sliding window prediction. The larger it is the faster but less accurate '
                             'the prediction. Default: 0.5. Cannot be larger than 1. We recommend the default.')
    parser.add_argument('--disable_tta', action='store_true', required=False, default=True,
                        help='Set this flag to disable test time data augmentation in the form of mirroring. Faster, '
                             'but less accurate inference.')
    parser.add_argument('--verbose', action='store_true', help="Set this if you like being talked to. You will have "
                                                               "to be a good listener/reader.")
    parser.add_argument('--save_probabilities', action='store_true',
                        help='Set this to export predicted class "probabilities". Required if you want to ensemble '
                             'multiple configurations.')
    parser.add_argument('--continue_prediction', action='store_true',
                        help='Continue an aborted previous prediction (will not overwrite existing files)')
    parser.add_argument('-chk', type=str, required=False, default='checkpoint_final.pth',
                        help='Name of the checkpoint you want to use. Default: checkpoint_final.pth')
    parser.add_argument('-npp', type=int, required=False, default=3,
                        help='Number of processes used for preprocessing. More is not always better. Beware of '
                             'out-of-RAM issues. Default: 3')
    parser.add_argument('-nps', type=int, required=False, default=3,
                        help='Number of processes used for segmentation export. More is not always better. Beware of '
                             'out-of-RAM issues. Default: 3')
    parser.add_argument('-prev_stage_predictions', type=str, required=False, default=None,
                        help='Folder containing the predictions of the previous stage. Required for cascaded models.')
    parser.add_argument('-num_parts', type=int, required=False, default=1,
                        help='Number of separate nnSeq2Seqv2_predict call that you will be making. Default: 1 (= this one '
                             'call predicts everything)')
    parser.add_argument('-part_id', type=int, required=False, default=0,
                        help='If multiple nnSeq2Seqv2_predict exist, which one is this? IDs start with 0 can end with '
                             'num_parts - 1. So when you submit 5 nnSeq2Seqv2_predict calls you need to set -num_parts '
                             '5 and use -part_id 0, 1, 2, 3 and 4. Simple, right? Note: You are yourself responsible '
                             'to make these run on separate GPUs! Use CUDA_VISIBLE_DEVICES (google, yo!)')
    parser.add_argument('-device', type=str, default='cuda', required=False,
                        help="Use this to set the device the inference should run with. Available options are 'cuda' "
                             "(GPU), 'cpu' (CPU) and 'mps' (Apple M1/M2). Do NOT use this to set which GPU ID! "
                             "Use CUDA_VISIBLE_DEVICES=X nnSeq2Seqv2_predict [...] instead!")
    parser.add_argument('--disable_progress_bar', action='store_true', required=False, default=False,
                        help='Set this flag to disable progress bar. Recommended for HPC environments (non interactive '
                             'jobs)')

    print(
        "\n#######################################################################\nPlease cite the following paper "
        "when using nnSeq2Seq:\n"
        "[1] Han L, Tan T, Zhang T, et al. "
        "Synthesis-based imaging-differentiation representation learning for multi-sequence 3D/4D MRI[J]. "
        "Medical Image Analysis, 2024, 92: 103044.\n"
        "[2] Han L, Zhang T, Huang Y, et al. "
        "An Explainable Deep Framework: Towards Task-Specific Fusion for Multi-to-One MRI Synthesis[C]. "
        "International Conference on Medical Image Computing and Computer-Assisted Intervention. Cham: Springer Nature Switzerland, 2023: 45-55.\n"
        "#######################################################################\n",)

    args = parser.parse_args()
    args.f = [i if i == 'all' else int(i) for i in args.f]

    model_folder = get_output_folder(args.d, args.tr, args.p, args.c)

    if not isdir(args.o):
        maybe_mkdir_p(args.o)

    # slightly passive aggressive haha
    assert args.part_id < args.num_parts, 'Do you even read the documentation? See nnSeq2Seqv2_predict -h.'

    assert args.device in ['cpu', 'cuda',
                           'mps'], f'-device must be either cpu, mps or cuda. Other devices are not tested/supported. Got: {args.device}.'
    if args.device == 'cpu':
        # let's allow torch to use hella threads
        import multiprocessing
        torch.set_num_threads(multiprocessing.cpu_count())
        device = torch.device('cpu')
    elif args.device == 'cuda':
        # multithreading in torch doesn't help nnSeq2Seq if run on GPU
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        device = torch.device('cuda')
    else:
        device = torch.device('mps')

    predictor = nnSeq2SeqPredictor(tile_step_size=args.step_size,
                                use_gaussian=True,
                                use_mirroring=not args.disable_tta,
                                perform_everything_on_device=True,
                                device=device,
                                verbose=args.verbose,
                                verbose_preprocessing=args.verbose,
                                allow_tqdm=not args.disable_progress_bar)
    predictor.initialize_from_trained_model_folder(
        model_folder,
        args.f,
        checkpoint_name=args.chk
    )
    predictor.predict_from_files(args.i, args.o, save_probabilities=args.save_probabilities,
                                 overwrite=not args.continue_prediction,
                                 num_processes_preprocessing=args.npp,
                                 num_processes_segmentation_export=args.nps,
                                 folder_with_segs_from_prev_stage=args.prev_stage_predictions,
                                 num_parts=args.num_parts,
                                 part_id=args.part_id)
    # r = predict_from_raw_data(args.i,
    #                           args.o,
    #                           model_folder,
    #                           args.f,
    #                           args.step_size,
    #                           use_gaussian=True,
    #                           use_mirroring=not args.disable_tta,
    #                           perform_everything_on_device=True,
    #                           verbose=args.verbose,
    #                           save_probabilities=args.save_probabilities,
    #                           overwrite=not args.continue_prediction,
    #                           checkpoint_name=args.chk,
    #                           num_processes_preprocessing=args.npp,
    #                           num_processes_segmentation_export=args.nps,
    #                           folder_with_segs_from_prev_stage=args.prev_stage_predictions,
    #                           num_parts=args.num_parts,
    #                           part_id=args.part_id,
    #                           device=device)


if __name__ == '__main__':
    predict_entry_point()
    """ # predict a bunch of files
    from nnseq2seq.paths import nnSeq2Seq_results, nnSeq2Seq_raw

    predictor = nnSeq2SeqPredictor(
        tile_step_size=0.5,
        use_gaussian=True,
        use_mirroring=True,
        perform_everything_on_device=True,
        device=torch.device('cuda', 0),
        verbose=False,
        verbose_preprocessing=False,
        allow_tqdm=True
    )
    predictor.initialize_from_trained_model_folder(
        join(nnSeq2Seq_results, 'Dataset003_Liver/nnSeq2SeqTrainer__nnSeq2SeqPlans__3d_lowres'),
        use_folds=(0,),
        checkpoint_name='checkpoint_final.pth',
    )
    predictor.predict_from_files(join(nnSeq2Seq_raw, 'Dataset003_Liver/imagesTs'),
                                 join(nnSeq2Seq_raw, 'Dataset003_Liver/imagesTs_predlowres'),
                                 save_probabilities=False, overwrite=False,
                                 num_processes_preprocessing=2, num_processes_segmentation_export=2,
                                 folder_with_segs_from_prev_stage=None, num_parts=1, part_id=0)

    # predict a numpy array
    from nnseq2seq.imageio.simpleitk_reader_writer import SimpleITKIO

    img, props = SimpleITKIO().read_images([join(nnSeq2Seq_raw, 'Dataset003_Liver/imagesTr/liver_63_0000.nii.gz')])
    ret = predictor.predict_single_npy_array(img, props, None, None, False)

    iterator = predictor.get_data_iterator_from_raw_npy_data([img], None, [props], None, 1)
    ret = predictor.predict_from_data_iterator(iterator, False, 1)

    # predictor = nnSeq2SeqPredictor(
    #     tile_step_size=0.5,
    #     use_gaussian=True,
    #     use_mirroring=True,
    #     perform_everything_on_device=True,
    #     device=torch.device('cuda', 0),
    #     verbose=False,
    #     allow_tqdm=True
    #     )
    # predictor.initialize_from_trained_model_folder(
    #     join(nnSeq2Seq_results, 'Dataset003_Liver/nnSeq2SeqTrainer__nnSeq2SeqPlans__3d_cascade_fullres'),
    #     use_folds=(0,),
    #     checkpoint_name='checkpoint_final.pth',
    # )
    # predictor.predict_from_files(join(nnSeq2Seq_raw, 'Dataset003_Liver/imagesTs'),
    #                              join(nnSeq2Seq_raw, 'Dataset003_Liver/imagesTs_predCascade'),
    #                              save_probabilities=False, overwrite=False,
    #                              num_processes_preprocessing=2, num_processes_segmentation_export=2,
    #                              folder_with_segs_from_prev_stage='/media/isensee/data/nnSeq2Seq_raw/Dataset003_Liver/imagesTs_predlowres',
    #                              num_parts=1, part_id=0) """