import os
import sys
sys.path.append(os.getcwd())
import torch

import json
import pickle
l = pickle.load(open("External/raw_em_E.pk", "rb"))

def score():
    rst = []
    correct = 0
    n = 0
    for d in l:
        logits = d['rel_logits']
        for lg in logits:
            rst.append(torch.tensor(lg).softmax(0)[-1].item())
    return rst

rst = score()
rst = sorted(rst)
json.dump(rst, open("External/score.json", "w"))

