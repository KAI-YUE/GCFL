import logging

# PyTorch libraries
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim import Optimizer

# My libraries
import grace_fl.constant as const
from deeplearning import UserDataset

class LocalUpdater(object):
    def __init__(self, user_resource):
        """Construct a local updater for a user.

        Args:
            lr (float):             learning rate for the user.
            batchSize (int):        batch size for the user. 
            localEpoch (int):       training epochs for the user.
            device (str):           set 'cuda' or 'cpu' for the user. 
            images (torch.Tensor):  training images of the user.
            labels (torch.Tensor):  training labels of the user.
        """
        
        
        try:
            self.batchSize = user_resource["batch_size"]
            self.device = user_resource["device"]

            assert("images" in user_resource)
            assert("labels" in user_resource)
        except KeyError:
            logging.error("LocalUpdater Initialization Failure! Input should include `lr`, `batchSize`!") 
        except AssertionError:
            logging.error("LocalUpdater Initialization Failure! Input should include samples!") 

        self.sampleLoader = DataLoader(UserDataset(user_resource["images"], user_resource["labels"]), 
                                batch_size=self.batchSize
                            )
        self.criterion = nn.CrossEntropyLoss()

    def local_step(self, model, optimizer, **kwargs):

        # localEpoch and iteration is set to 1
        for sample in self.sampleLoader:
            
            image = sample["image"].to(self.device)
            label = sample["label"].to(self.device)

            output = model(image)
            loss = self.criterion(output, label)
            loss.backward()
            optimizer.gather(**kwargs)

class _graceOptimizer(Optimizer):
    """
    A warpper optimizer gather gradients from local users and overwrite 
    step() method for the server.

    Args:
        params (nn.Module.parameters): model learnable parameters.
    """
    def __init__(self, params, grace, **kwargs):
        super(self.__class__, self).__init__(params)
        self.rawBits = 0
        self.encodedBit = 0
        self.grace = grace

        self._gatheredGradients = []
        for group in self.param_groups:
            for param in group["params"]:
                self._gatheredGradients.append(torch.zeros_like(param))

    def gather(self, **kwargs):
        """Gather local gradients.
        """
        for group in self.param_groups:
            for i, param in enumerate(group['params']):
                if param.grad is None:
                    continue
                    
                encodedTensor = self.grace.compress(param.grad.data)
                self._gatheredGradients[i] += self.grace.decompress(encodedTensor, shape=param.grad.data.shape)                
                
                # clear the gradients for next step, which is equivalent to zero_grad()
                param.grad.detach_()
                param.grad.zero_() 

    def step(self, **kwargs):
        """Performs a single optimization step.
        """
        for group in self.param_groups:
            for i, param in enumerate(group['params']):

                d_param = self.grace.trans_aggregation(self._gatheredGradients[i], **kwargs)
                param.data.add_(d_param, alpha=-group['lr'])
                self._gatheredGradients[i].zero_()

class _predOptimizer(Optimizer):
    """
    A warpper optimizer which implements predictive encoding with turn trick.
    It gather gradients residuals ("+" residual for even turns and "-" residual for 
    odd turns) from local users and overwrite step() method for the server.

    Args:
        params (nn.Module.parameters): model learnable parameters.
    """
    def __init__(self, params, grace, **kwargs):
        super(self.__class__, self).__init__(params)
        self.grace = grace

        self._buffer_empty = True
        self._gatheredGradients = []
        self._buffer = []

        for group in self.param_groups:
            for param in group["params"]:
                self._gatheredGradients.append(torch.zeros_like(param))
                self._buffer.append(torch.zeros_like(param))

    def gather(self, **kwargs):
        """Gather local gradients.
        """
        for group in self.param_groups:
            momentum = group["momentum"]
            for i, param in enumerate(group['params']):
                if param.grad is None:
                    continue

                # if buffer is empty, encode the gradient
                if self._buffer_empty:
                    encodedTensor = self.grace.compress(param.grad.data)
                    self._gatheredGradients[i] += self.grace.decompress(encodedTensor, shape=param.grad.data.shape)
                # if buffer is nonempty, encode the residual
                else:
                    encodedTensor = self.grace.compress_with_reference(param.grad.data, self._buffer[i])
                    self._gatheredGradients[i] += self.grace.decompress_with_reference(encodedTensor, self._buffer[i])

                # clear the gradients for next step, which is equivalent to zero_grad()
                param.grad.detach_()
                param.grad.zero_() 


    def step(self):
        """Performs a single optimization step.
        """
        for group in self.param_groups:
            momentum = group["momentum"]

            for i, param in enumerate(group['params']):
                d_param = self.grace.trans_aggregation(self._gatheredGradients[i])
                
                if momentum != 0:
                    param_state = self.state[param]
                    if 'momentum_buffer' not in param_state:
                        buf = param_state['momentum_buffer'] = torch.clone(d_param).detach()
                    else:
                        buf = param_state['momentum_buffer']
                        buf.mul_(momentum).add_(d_param)

                    # compress the broadcast tensor
                    if self._buffer_empty:
                        encodedTensor = self.grace.compress(d_param)
                        d_param = self.grace.decompress(encodedTensor, shape=param.data.shape)
                    # if buffer is nonempty, encode the residual
                    else:
                        # ones_tensor = torch.ones_like(param)
                        # d_param = torch.where(buf>0, ones_tensor, -ones_tensor)
                        encodedTensor = self.grace.compress_with_reference(d_param, self._buffer[i])
                        d_param = self.grace.decompress_with_reference(encodedTensor, self._buffer[i])
            
                param.data.add_(d_param, alpha=-group["lr"])
                self._gatheredGradients[i].zero_()
                
                # register buffer
                self._buffer[i] = torch.clone(d_param).detach()

        self._buffer_empty = False


class _predTurnOptimizer(Optimizer):
    """
    A warpper optimizer which implements predictive encoding with turn trick.
    It gather gradients residuals ("+" residual for even turns and "-" residual for 
    odd turns) from local users and overwrite step() method for the server.

    Args:
        params (nn.Module.parameters): model learnable parameters.
    """
    def __init__(self, params, grace, **kwargs):
        super(self.__class__, self).__init__(params)
        self.grace = grace

        self._current_sign = 1
        self._gatheredGradients = []
        self._plus_sign_buffer = []
        self._minus_sign_buffer = []
        self._buffer_empty = True
        for group in self.param_groups:
            for param in group["params"]:
                self._gatheredGradients.append(torch.zeros_like(param))
                self._plus_sign_buffer.append(torch.zeros_like(param))
                self._minus_sign_buffer.append(torch.zeros_like(param))

    def gather(self, **kwargs):
        """Gather local gradients.
        """
        try:
            self.turn = kwargs["turn"]
            self._current_sign = 1 if self.turn%2 == 0 else -1
        except KeyError:
            logging.error("Turn trick cannot be applied without 'turn' parameters.")

        for group in self.param_groups:
            for i, param in enumerate(group['params']):
                if param.grad is None:
                    continue

                # if buffer is empty, encode the gradient
                if self._buffer_empty:
                    encodedTensor = self.grace.compress(param.grad.data, sign=self._current_sign)
                    self._gatheredGradients[i] += self.grace.decompress(encodedTensor, shape=param.grad.data.shape)
                # if buffer in nonempty, encode the residual
                elif self.current_sign == 1:
                    encodedTensor = self.grace.compress_with_reference(param.grad, self._plus_sign_buffer[i])
                    self._gatheredGradients[i] += self.grace.decompress_with_reference(encodedTensor, self._plus_sign_buffer[i])
                else:
                    encodedTensor = self.grace.compress_with_reference(param.grad, self._minus_sign_buffer[i])
                    self._gatheredGradients[i] += self.grace.decompress_with_reference(encodedTensor, self._minus_sign_buffer[i])

                if self.current_sign == 1:
                    self._minus_sign_buffer[i] += (param.grad.data < -const.EPSILON)
                else:
                    self._plus_sign_buffer[i] += (param.grad.data > const.EPSILON)

                # clear the gradients for next step, which is equivalent to zero_grad()
                param.grad.detach_()
                param.grad.zero_() 


    def step(self):
        """Performs a single optimization step.
        """
        for group in self.param_groups:
            for i, param in enumerate(group['params']):
                d_param = self.grace.trans_aggregation(self._gatheredGradients[i])

                # register buffer
                if self.current_sign == 1:
                    self._plus_sign_buffer[i].zero_()
                    self._minus_sign_buffer[i] = -self.grace.trans_aggregation(self._minus_sign_buffer[i], -self.current_sign)            
                else:
                    self._minus_sign_buffer[i].zero_()
                    self._plus_sign_buffer[i] = self.grace.trans_aggregation(self._plus_sign_buffer[i], -self.current_sign)
                
                d_param = self.current_sign * d_param
                param.data.add_(d_param, alpha=-group["lr"])
                self._gatheredGradients[i].zero_()
                
        self._buffer_empty = False

    @property
    def current_sign(self):
        """wrapper of the grace._current_sign"""
        return self.grace._current_sign

    def set_current_sign(self, sign):
        """set wrapper of the grace._current_sign"""
        self.grace._current_sign = sign

def grace_optimizer(optimizer, grace, **kwargs):
    """
    An optimizer that wraps another torch.optim.Optimizer.

    Allreduce operations are executed after each gradient is computed by ``loss.backward()``
    in parallel with each other. The ``step()`` method ensures that all allreduce operations are
    finished before applying gradients to the model.

    Args:
        optimizer (torch.nn.optim.Optimizer):   Optimizer to use for computing gradients and applying updates.
        grace (grace_fl.Compressor):            Compression algorithm used during allreduce to reduce the amount
        mode (int):                             mode represents different implementations of optimizer.
    """
    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override the `step()` method.

    """>>> TODO: add another 2 modes"""
    if "mode" in kwargs:
        mode = kwargs["mode"]
    else:
        mode = 3

    if mode==0:
        cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
        dict(_predTurnOptimizer.__dict__))
    elif mode == 1:
        cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
        dict(_predOptimizer.__dict__))
    elif mode == 3:
        cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
            dict(_graceOptimizer.__dict__))

    return cls(optimizer.param_groups, grace, **kwargs)

class _signSGD(Optimizer):
    """
    A warpper optimizer gather gradients from local users and overwrite 
    step() method for the server.
    Args:
        params (nn.Module.parameters): model learnable parameters.
    """
    def __init__(self, params):
        super(self.__class__, self).__init__(params)
    
    @torch.no_grad()
    def step(self, **kwargs):
        """Performs a single optimization step.
        """
        for group in self.param_groups:
            for param in group['params']:
                
                if param.grad is None:
                    continue
                
                ones_tensor = torch.ones_like(param)
                d_p = torch.where(param.grad>0, ones_tensor, -ones_tensor)
                
                param.add_(d_p, alpha=-group["lr"])

def signSGD(optimizer):
    """
    An optimizer that wraps another torch.optim.Optimizer.
    Allreduce operations are executed after each gradient is computed by ``loss.backward()``
    in parallel with each other. The ``step()`` method ensures that all allreduce operations are
    finished before applying gradients to the model.
    Args:
        optimizer (torch.nn.optim.Optimizer):   Optimizer to use for computing gradients and applying updates.
    """
    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override the `step()` method.


    cls = type(optimizer.__class__.__name__, (optimizer.__class__,), dict(_signSGD.__dict__))

    return cls(optimizer.param_groups)