"""
Copyright 2020- Kai.Lib
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import random
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from models.beam import Beam
from .attention import Attention

if torch.cuda.is_available():
    import torch.cuda as device
else:
    import torch as device


class Speller(nn.Module):
    """
    Converts higher level features (from listener) into output utterances by specifying a probability distribution over sequences of characters.

    Args:
        vocab_size (int): size of the vocabulary
        max_len (int): a maximum allowed length for the sequence to be processed
        hidden_size (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        layer_size (int, optional): number of recurrent layers (default: 1)
        rnn_cell (str, optional): type of RNN cell (default: gru)
        bidirectional (bool, optional): if the listener is bidirectional (default False)
        input_dropout_p (float, optional): dropout probability for the input sequence (default: 0)
        dropout_p (float, optional): dropout probability for the output sequence (default: 0)
        use_attention(bool, optional): flag indication whether to use attention mechanism or not (default: false)
    Inputs: inputs, listener_hidden, listener_outputs, function, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **listener_hidden** (num_layers * num_directions, batch_size, hidden_size): tensor containing the features in the
          hidden state `h` of listener. Used as the initial hidden state of the decoder. (default `None`)
        - **listener_outputs** (batch, seq_len, hidden_size): tensor with containing the outputs of the listener.
          Used for attention mechanism (default is `None`).
        - **function** (torch.nn.Module): A function used to generate symbols from RNN hidden state
          (default is `torch.nn.functional.log_softmax`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).
    Outputs: speller_outputs, speller_hidden, ret_dict
        - **speller_outputs** (seq_len, batch, vocab_size): list of tensors with size (batch_size, vocab_size) containing
          the outputs of the decoding function.
    """

    def __init__(self, vocab_size, max_len, hidden_size, sos_id, eos_id,
                 layer_size=1, rnn_cell='gru', dropout_p=0, use_attention=True,
                 device=None, use_beam_search=False, k=8):
        super(Speller, self).__init__()
        if rnn_cell.lower() != 'gru' and rnn_cell.lower() != 'lstm':
            raise ValueError("Unsupported RNN Cell: %s" % rnn_cell)
        self.rnn_cell = nn.LSTM if rnn_cell.lower() == 'lstm' else nn.GRU if rnn_cell.lower() == 'gru' else nn.RNN
        self.rnn = self.rnn_cell(hidden_size , hidden_size, layer_size, batch_first=True, dropout=dropout_p)
        self.output_size = vocab_size
        self.max_length = max_len
        self.use_attention = use_attention
        self.eos_id = eos_id
        self.sos_id = sos_id
        self.hidden_size = hidden_size
        self.embedding = nn.Embedding(self.output_size, self.hidden_size)
        self.out = nn.Linear(self.hidden_size, self.output_size)
        self.layer_size = layer_size
        self.input_dropout = nn.Dropout(p=dropout_p)
        self.device = device
        self.use_beam_search = use_beam_search
        self.k = k
        if use_attention:
            self.attention = Attention(self.hidden_size)

    def forward_step(self, speller_input, speller_hidden, listener_outputs, function):
        """
        :param speller_input: labels (except </s>)
        :param speller_hidden: hidden state of speller
        :param listener_outputs: output of listener
        :param function: decode function
        """
        batch_size = speller_input.size(0)   # speller_input.size(0) : batch_size
        output_size = speller_input.size(1)  # speller_input.size(1) : seq_len
        embedded = self.embedding(speller_input)
        embedded = self.input_dropout(embedded)
        if self.training:
            self.rnn.flatten_parameters()
        speller_output, hidden = self.rnn(embedded, speller_hidden) # speller output
        attn = None
        if self.use_attention:
            output = self.attention(decoder_output=speller_output, encoder_output=listener_outputs)
        else: output = speller_output
        # torch.view()에서 -1이면 나머지 알아서 맞춰줌
        predicted_softmax = function(self.out(output.contiguous().view(-1, self.hidden_size)), dim=1).view(batch_size, output_size, -1)
        return predicted_softmax

    def forward(self, inputs=None, listener_hidden=None, listener_outputs=None, function=F.log_softmax, teacher_forcing_ratio=0.99):
        """
        :param inputs: targets
        :param listener_hidden: hidden state of listener
        :param listener_outputs:  last hidden state of listener
        :param function: decode function
        :param teacher_forcing_ratio: ratio of teacher forcing
        """
        decode_results = []
        # Validate Arguments
        inputs, batch_size, max_length = self._validate_args(inputs, listener_hidden, listener_outputs, teacher_forcing_ratio)
        # Initiate Speller Hidden State to zeros  :  LxBxH
        speller_hidden = torch.FloatTensor(self.layer_size, batch_size, self.hidden_size).uniform_(-1.0, 1.0)#.cuda()
        # Decide Use Teacher Forcing or Not
        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        # Manual unrolling is used to support random teacher forcing.
        # If teacher_forcing_ratio is True or False instead of a probability, the unrolling can be done in graph
        if use_teacher_forcing:
            speller_input = inputs[:, :-1] # except </s>
            # (batch_size, seq_len, classfication_num)
            predicted_softmax = self.forward_step(speller_input, speller_hidden, listener_outputs, function=function)
            """Extract Output by Step"""
            for di in range(predicted_softmax.size(1)):
                step_output = predicted_softmax[:, di, :]
                decode_results.append(step_output)
        else:
            if self.use_beam_search:
                """Implementation of Beam-Search Decoding"""
                pass
            else:
                speller_input = inputs[:, 0].unsqueeze(1)
                for di in range(max_length):
                    predicted_softmax = self.forward_step(speller_input, speller_hidden, listener_outputs, function=function)
                    # (batch_size, classfication_num)
                    step_output = predicted_softmax.squeeze(1)
                    decode_results.append(step_output)
                    speller_input = decode_results[-1].topk(1)[1]

        logit = torch.stack(decode_results, dim=1).to(self.device)
        y_hat = logit.max(-1)[1]

        return y_hat, logit

    def _validate_args(self, inputs, listener_hidden, listener_outputs, teacher_forcing_ratio):
        if self.use_attention:
            if listener_outputs is None:
                raise ValueError("Argument listener_outputs cannot be None when attention is used.")

        # inference batch size
        if inputs is None and listener_hidden is None:
            batch_size = 1
        else:
            if inputs is not None:
                batch_size = inputs.size(0)
            else:
                if self.rnn_cell is nn.LSTM:
                    batch_size = listener_hidden[0].size(1)
                elif self.rnn_cell is nn.GRU:
                    batch_size = listener_hidden.size(1)

        # set default input and max decoding length
        if inputs is None:
            if teacher_forcing_ratio > 0:
                raise ValueError("Teacher forcing has to be disabled (set 0) when no inputs is provided.")
            inputs = torch.LongTensor([self.sos_id] * batch_size).view(batch_size, 1)
            if torch.cuda.is_available():
                inputs = inputs.cuda()
            max_length = self.max_length
        else:
            max_length = inputs.size(1) - 1 # minus the start of sequence symbol

        return inputs, batch_size, max_length