import  os
import pickle
import logging
import numpy as np

# PyTorch libraries
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

# My libraries
from config import load_config
from deeplearning import nn_registry
from grace_fl import compressor_registry
from grace_fl.gc_optimizer import signSGD, grace_optimizer, LocalUpdater
from deeplearning.dataset import UserDataset, assign_user_data, assign_user_resource

def init_logger(config):
    """Initialize a logger object. 
    """
    log_level = config.log_level
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)

    fh = logging.FileHandler(config.log_file)
    fh.setLevel(log_level)
    sh = logging.StreamHandler()
    sh.setLevel(log_level)

    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.info("-"*80)

    return logger

def parse_config(config):
    if config.predictive and config.take_turns:
        mode = 0
    elif config.predictive:
        mode = 1
    elif config.take_turns:
        mode = 2
    else:
        mode = 3

    return mode

def save_record(file_path, record):
    current_path = os.path.dirname(__file__)
    with open(os.path.join(current_path, file_path), "wb") as fp:
        pickle.dump(record, fp)

def test_accuracy(model, test_dataset, device="cuda"):
    
    server_dataset = UserDataset(test_dataset["images"], test_dataset["labels"])
    num_samples = test_dataset["labels"].shape[0]

    # Full Batch testing
    testing_data_loader = DataLoader(dataset=server_dataset, batch_size=len(server_dataset))
    for samples in testing_data_loader:
        results = model(samples["image"].to(device))
    
    predicted_labels = torch.argmax(results, dim=1).detach().cpu().numpy()
    accuracy = np.sum(predicted_labels == test_dataset["labels"]) / num_samples

    return accuracy

def train_accuracy(model, train_dataset, device="cuda"):

    server_dataset = UserDataset(train_dataset["images"], train_dataset["labels"])
    num_samples = train_dataset["labels"].shape[0]

    # Full Batch testing
    training_data_loader = DataLoader(dataset=server_dataset, batch_size=len(server_dataset))
    for samples in training_data_loader:
        results = model(samples["image"].to(device))
    
    predicted_labels = torch.argmax(results, dim=1).detach().cpu().numpy()
    accuracy = np.sum(predicted_labels == train_dataset["labels"]) / num_samples

    return accuracy

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def train(config, logger, record):
    """Simulate Federated Learning training process. 
    
    Args:
        config (object class)
    """
    # initialize the model
    sample_size = config.sample_size[0] * config.sample_size[1]
    classifier = nn_registry[config.model](dim_in=sample_size, dim_out=config.classes)
    classifier.to(config.device)
    
    # Parse the configuration and fetch mode code for the optimizer
    mode = parse_config(config)

    # number of trainable parameters
    record["num_parameters"] = count_parameters(classifier)

    # initialize data record 
    record["compress_ratio"] = []
    record["testing_accuracy"] = []

    # initialize userIDs
    if config.random_sampling:
        users_to_sample = int(config.users * config.sampling_fraction)
        userIDs = np.arange(config.users) 

    # initialize the optimizer for the server model
    optimizer = optim.SGD(params=classifier.parameters(), lr=config.lr, momentum=config.momentum)
    grace = compressor_registry[config.compressor](config)
    optimizer = grace_optimizer(optimizer, grace, mode=mode) # wrap the optimizer
    criterion = nn.CrossEntropyLoss()

    dataset = assign_user_data(config)
    iterations_per_epoch = np.ceil((dataset["train_data"]["images"].shape[0] * config.sampling_fraction) / config.local_batch_size)
    iterations_per_epoch = iterations_per_epoch.astype(np.int)
    
    global_turn = -1
    break_flag = False
    comm_rounds = 0

    for epoch in range(config.epoch):
        logger.info("epoch {:02d}".format(epoch))
        
        for iteration in range(iterations_per_epoch):
            global_turn += 1
            # sample a fraction of users randomly
            if config.random_sampling:
                np.random.shuffle(userIDs)
                userIDs_candidates = userIDs[:users_to_sample]

            # Wait for all users aggregating gradients
            for userID in userIDs_candidates:
                user_resource = assign_user_resource(config, userID, 
                                    dataset["train_data"],  
                                    dataset["user_with_data"]
                                )

                updater = LocalUpdater(user_resource)
                updater.local_step(classifier, optimizer, turn=global_turn)
            
            optimizer.step()

        with torch.no_grad():

            # validate the model and log test accuracy
            testAcc = test_accuracy(classifier, dataset["test_data"], device=config.device)
            record["testing_accuracy"].append(testAcc)
            logger.info("Test accuracy {:.4f}".format(testAcc))
            comm_rounds += 1
            # comm_rounds += iterations_per_epoch

            if testAcc > config.performance_threshold:
                break_flag = True
                break

        record["compress_ratio"].append(optimizer.grace.compress_ratio)
        logger.info("compression ratio: {:.4f}".format(record["compress_ratio"][-1]))
        optimizer.grace.reset()

        if break_flag == True:
            logger.info("Total rounds {:d}".format(comm_rounds))
            record["comm_rounds"] = comm_rounds
            break

def main():
    config = load_config()
    logger = init_logger(config)
    record = {}
    train(config, logger, record)
    save_record(config.record_dir, record)

if __name__ == "__main__":
    main()


