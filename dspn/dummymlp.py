import torch
import torch.nn as nn

class MLP(nn.Module):
    def __init__(self, embedding_dim, hidden_dim, label_dim):
        super(MLP, self).__init__()

        self.layers = nn.Sequential(
            nn.Linear(in_features=embedding_dim, out_features=hidden_dim),
            nn.ReLU(),
            nn.Linear(in_features=hidden_dim, out_features=1)
        )
    
    def forward(self, input_):
        return self.layers(input_)