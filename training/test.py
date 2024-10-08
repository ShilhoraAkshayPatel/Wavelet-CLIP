"""
eval pretained model.
"""

import numpy as np
import random
import yaml
from tqdm import tqdm
from metrics.utils import get_test_metrics
import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.utils.data
from dataset.abstract_dataset import DeepfakeAbstractBaseDataset
from detectors import DETECTOR
import argparse

parser = argparse.ArgumentParser(description='Process some paths.')
parser.add_argument(
    "--detector_path",
    type=str,
    default="./training/config/detector/clip.yaml",
    help="path to detector YAML file",
)
parser.add_argument("--test_dataset", nargs="+")
parser.add_argument("--weights_path", type=str, default="./training/weights")
# parser.add_argument("--lmdb", action='store_true', default=False)
args = parser.parse_args()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def init_seed(config):
    if config['manualSeed'] is None:
        config['manualSeed'] = random.randint(1, 10000)
    random.seed(config['manualSeed'])
    torch.manual_seed(config['manualSeed'])
    if config['cuda']:
        torch.cuda.manual_seed_all(config['manualSeed'])


def prepare_testing_data(config):
    def get_test_data_loader(config, test_name):
        # update the config dictionary with the specific testing dataset
        config = config.copy()  # create a copy of config to avoid altering the original one
        config['test_dataset'] = test_name  # specify the current test dataset
        test_set = DeepfakeAbstractBaseDataset(
                config=config,
                mode='test', 
            )
        test_data_loader = \
            torch.utils.data.DataLoader(
                dataset=test_set, 
                batch_size=config['test_batchSize'],
                shuffle=False, 
                num_workers=int(config['workers']),
                collate_fn=test_set.collate_fn,
                drop_last=False
            )
        return test_data_loader

    test_data_loaders = {}
    for one_test_name in config['test_dataset']:
        test_data_loaders[one_test_name] = get_test_data_loader(config, one_test_name)
    return test_data_loaders


def choose_metric(config):
    metric_scoring = config['metric_scoring']
    if metric_scoring not in ['eer', 'auc', 'acc', 'ap']:
        raise NotImplementedError('metric {} is not implemented'.format(metric_scoring))
    return metric_scoring


def test_one_dataset(model, data_loader):
    cls_prediction_lists = []
    prediction_lists = []
    feature_lists = []
    label_lists = []
    for i, data_dict in tqdm(enumerate(data_loader), total=len(data_loader)):
        # get data
        data, label, mask, landmark = \
        data_dict['image'], data_dict['label'], data_dict['mask'], data_dict['landmark']
        label = torch.where(data_dict['label'] != 0, 1, 0)
        # move data to GPU
        data_dict['image'], data_dict['label'] = data.to(device), label.to(device)
        if mask is not None:
            data_dict['mask'] = mask.to(device)
        if landmark is not None:
            data_dict['landmark'] = landmark.to(device)

        # model forward without considering gradient computation
        predictions = inference(model, data_dict)
        label_lists += list(data_dict['label'].cpu().detach().numpy())
        prediction_lists += list(predictions['prob'].cpu().detach().numpy())
        cls_prediction_lists += list(predictions['cls'].cpu().detach().numpy())
        feature_lists += list(predictions['feat'].cpu().detach().numpy())

    return np.array(prediction_lists), np.array(cls_prediction_lists), np.array(label_lists), np.array(feature_lists)

def test_epoch(model, test_data_loaders):
    # set model to eval mode
    model.eval()

    # define test recorder
    metrics_all_datasets = {}

    # testing for all test data
    keys = test_data_loaders.keys()
    for key in keys:
        data_dict = test_data_loaders[key].dataset.data_dict

        # compute loss for each dataset
        predictions_nps, cls_pred_nps, label_nps, feat_nps = test_one_dataset(model, test_data_loaders[key])

        # compute metric for each dataset
        metric_one_dataset = get_test_metrics(
            y_pred=predictions_nps, y_true=label_nps, img_names=data_dict["image"]
        )
        metrics_all_datasets[key] = metric_one_dataset

        # info for each dataset
        tqdm.write(f"dataset: {key}")
        for k, v in metric_one_dataset.items():
            tqdm.write(f"{k}: {v}")

    return metrics_all_datasets

@torch.no_grad()
def inference(model, data_dict):
    predictions = model(data_dict, inference=True)
    return predictions


def main():
    # parse options and load config
    with open(args.detector_path, 'r') as f:
        config = yaml.safe_load(f)
    with open('./training/config/test_config.yaml', 'r') as f:
        config2 = yaml.safe_load(f)
    if 'label_dict' in config:
        config2['label_dict']=config['label_dict']
    weights_path = None
    # If arguments are provided, they will overwrite the yaml settings
    if args.test_dataset:
        config['test_dataset'] = args.test_dataset
        config2['test_dataset'] = args.test_dataset
    if args.weights_path:
        config['weights_path'] = args.weights_path
        config2['weights_path'] = args.weights_path
        weights_path = args.weights_path
    
    # init seed
    init_seed(config)

    # set cudnn benchmark if needed
    if config['cudnn']:
        cudnn.benchmark = True
    # print('config', config)
    # print('config2', config2)
    # prepare the testing data loader
    new_config = {**config, **config2}
    test_data_loaders = prepare_testing_data(new_config)
    
    # prepare the model (detector)
    model_class = DETECTOR[config['model_name']]
    model = model_class(config).to(device)
    epoch = 0
    if weights_path:
        try:
            epoch = int(weights_path.split('/')[-1].split('.')[0].split('_')[2])
        except:
            epoch = 0
            
        ckpt = torch.load(weights_path, map_location=device)
        remove_prefix = 'module.'
        state_dict = {k[len(remove_prefix):] if k.startswith(remove_prefix) else k: v for k, v in ckpt.items()}
        model.load_state_dict(state_dict, strict=True)
        print('===> Load checkpoint done!')
    else:
        print('Fail to load the pre-trained weights')
    
    # start testing
    best_metric = test_epoch(model, test_data_loaders)
    print('===> Test Done!')

if __name__ == '__main__':
    main()
