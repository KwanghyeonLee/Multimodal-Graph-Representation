import copy

import torch
import torch.nn as nn
import torch.nn.functional as F

from maskrcnn_benchmark.modeling.make_layers import make_fc
from maskrcnn_benchmark.utils.comm import get_rank
from maskrcnn_benchmark.config import cfg
from maskrcnn_benchmark.modeling.roi_heads.relation_head.model_msg_passing import (
    PairwiseFeatureExtractor,
)
from maskrcnn_benchmark.modeling.roi_heads.relation_head.rel_proposal_network.models import (
    make_relation_confidence_aware_module,
)
from maskrcnn_benchmark.structures.boxlist_ops import squeeze_tensor
from torch.distributions.relaxed_bernoulli import RelaxedBernoulli
from torch.distributions.utils import clamp_probs

class QuantizeBernoulli(torch.autograd.Function):
    @staticmethod
    def forward(ctx, soft_value):
        hard_value = soft_value.round()
        hard_value._unquantize = soft_value
        return hard_value


class MessagePassingUnit_v2(nn.Module):
    def __init__(self, input_dim, filter_dim=128):
        super(MessagePassingUnit_v2, self).__init__()
        self.w = nn.Linear(input_dim, filter_dim, bias=True)
        self.fea_size = input_dim
        self.filter_size = filter_dim

    def forward(self, unary_term, pair_term):

        if unary_term.size()[0] == 1 and pair_term.size()[0] > 1:
            unary_term = unary_term.expand(pair_term.size()[0], unary_term.size()[1])
        if unary_term.size()[0] > 1 and pair_term.size()[0] == 1:
            pair_term = pair_term.expand(unary_term.size()[0], pair_term.size()[1])
        # print '[unary_term, pair_term]', [unary_term, pair_term]
        gate = self.w(F.relu(unary_term)) * self.w(F.relu(pair_term))
        gate = torch.sigmoid(gate.sum(1))
        # print 'gate', gate
        output = pair_term * gate.expand(gate.size()[0], pair_term.size()[1])

        return output, gate


def reverse_sigmoid(x):
    new_x = x.clone()
    new_x[x > 0.999] = x[x > 0.999] - (x[x > 0.999].clone().detach() - 0.999)
    new_x[x < 0.001] = x[x < 0.001] + (-x[x < 0.001].clone().detach() + 0.001)
    return torch.log((new_x) / (1 - (new_x)))


class MessagePassingUnit_v1(nn.Module):
    def __init__(self, input_dim, filter_dim=64):
        """

        Args:
            input_dim:
            filter_dim: the channel number of attention between the nodes
        """
        super(MessagePassingUnit_v1, self).__init__()
        self.w = nn.Sequential(
            nn.LayerNorm(input_dim * 2),
            nn.ReLU(),
            nn.Linear(input_dim * 2, filter_dim, bias=True),
        )

        self.fea_size = input_dim
        self.filter_size = filter_dim

        self.gate_weight = nn.Parameter(
            torch.Tensor(
                [
                    0.5,
                ]
            ),
            requires_grad=True,
        )
        self.aux_gate_weight = nn.Parameter(
            torch.Tensor(
                [
                    0.5,
                ]
            ),
            requires_grad=True,
        )

    def forward(self, unary_term, pair_term, aux_gate=None):

        if unary_term.size()[0] == 1 and pair_term.size()[0] > 1:
            unary_term = unary_term.expand(pair_term.size()[0], unary_term.size()[1])
        if unary_term.size()[0] > 1 and pair_term.size()[0] == 1:
            pair_term = pair_term.expand(unary_term.size()[0], pair_term.size()[1])
        paired_feats = torch.cat([unary_term, pair_term], 1)

        gate = torch.sigmoid(self.w(paired_feats))
        if gate.shape[1] > 1:
            gate = gate.mean(1)  # average the nodes attention between the nodes
        if aux_gate is not None:


            gate = gate * aux_gate
        # print 'gate', gate
        output = pair_term * gate.view(-1, 1).expand(gate.size()[0], pair_term.size()[1])

        return output, gate


class MessagePassingUnitGatingWithRelnessLogits(nn.Module):
    def __init__(
        self, input_dim, auxiliary_dim, use_auxiliary_gate_weight=False, filter_dim=64
    ):
        """

        Args:
            input_dim:
            filter_dim: the channel number of attention between the nodes
        """
        super(MessagePassingUnitGatingWithRelnessLogits, self).__init__()
        self.auxiliary_dim = auxiliary_dim

        self.w_aux = nn.Sequential(
            nn.LayerNorm(self.auxiliary_dim),
            nn.ReLU(),
            nn.Linear(self.auxiliary_dim, 8, bias=True),
        )

        self.w = nn.Sequential(
            nn.LayerNorm(input_dim * 2),
            nn.ReLU(),
            nn.Linear(input_dim * 2, filter_dim, bias=True),
        )
        if use_auxiliary_gate_weight:
            self.aux_gate_weight = nn.Parameter(
                torch.Tensor(
                    [
                        0.33,
                    ]
                ),
                requires_grad=True,
            )
            self.gate_weight = nn.Parameter(
                torch.Tensor(
                    [
                        0.33,
                    ]
                ),
                requires_grad=True,
            )
            self.aux_score_weight = nn.Parameter(
                torch.Tensor(
                    [
                        0.33,
                    ]
                ),
                requires_grad=True,
            )
        else:
            self.aux_gate_weight = nn.Parameter(
                torch.Tensor(
                    [
                        0.5,
                    ]
                ),
                requires_grad=True,
            )
            self.gate_weight = nn.Parameter(
                torch.Tensor(
                    [
                        0.5,
                    ]
                ),
                requires_grad=True,
            )
        self.use_auxiliary_gate_weight = use_auxiliary_gate_weight

        self.fea_size = input_dim
        self.filter_size = filter_dim

    def forward(self, unary_term, pair_term, auxiliary_term, auxiliary_gating_weight=None):

        if unary_term.size()[0] == 1 and pair_term.size()[0] > 1:
            unary_term = unary_term.expand(pair_term.size()[0], unary_term.size()[1])
        if unary_term.size()[0] > 1 and pair_term.size()[0] == 1:
            pair_term = pair_term.expand(unary_term.size()[0], pair_term.size()[1])
        paired_feats = F.relu(
            torch.cat(
                [
                    unary_term,
                    pair_term,
                ],
                1,
            )
        )

        gate = torch.sigmoid(self.w(paired_feats))
        aux_gate = torch.sigmoid(self.w_aux(auxiliary_term))

        if gate.shape[1] > 1:
            gate = gate.mean(1)  # average the nodes attention between the nodes
        if aux_gate.shape[1] > 1:
            aux_gate = aux_gate.mean(1)
        aux_gate = squeeze_tensor(aux_gate)
        gate = squeeze_tensor(gate)

        gate = self.gate_weight * reverse_sigmoid(
            gate
        ) + self.aux_gate_weight * reverse_sigmoid(aux_gate)

        if self.use_auxiliary_gate_weight:
            assert auxiliary_gating_weight is not None
            # sigmoid_reverse_gate = reverse_sigmoid(auxiliary_gating_weight)
            # gate += self.aux_score_weight * sigmoid_reverse_gate

            gate = torch.sigmoid(gate)
            gate = gate * auxiliary_gating_weight
        else:
            gate = torch.sigmoid(gate)
        # print 'gate', gate
        output = pair_term * gate.view(-1, 1).expand(gate.shape[0], pair_term.shape[1])

        return output, gate


class MessageFusion(nn.Module):
    def __init__(self, input_dim, dropout):
        super(MessageFusion, self).__init__()
        self.wih = nn.Linear(input_dim, input_dim, bias=True)
        self.whh = nn.Linear(input_dim, input_dim, bias=True)
        self.dropout = dropout

    def forward(self, input, hidden):
        output = self.wih(F.relu(input)) + self.whh(F.relu(hidden))
        if self.dropout: # False
            output = F.dropout(output, training=self.training)
        return output


class LearnableRelatednessGating(nn.Module):
    def __init__(self):
        super(LearnableRelatednessGating, self).__init__()
        cfg_weight = cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.LEARNABLE_SCALING_WEIGHT
        self.alpha = nn.Parameter(torch.Tensor([cfg_weight[0]]), requires_grad=True)
        self.beta = nn.Parameter(torch.Tensor([cfg_weight[1]]), requires_grad=False)

    def forward(self, relness):
        relness = torch.clamp(self.alpha * relness - self.alpha * self.beta, min=0, max=1.0)
        return relness


class BGNNContext(nn.Module):
    def __init__(
        self,
        cfg,
        in_channels,
        hidden_dim=1024,
        num_iter=2,
        dropout=False,
        gate_width=128,
        use_kernel_function=False,
    ):
        super(BGNNContext, self).__init__()
        self.cfg = cfg
        self.hidden_dim = hidden_dim
        self.update_step = num_iter

        if self.update_step < 1:
            print(
                "WARNING: the update_step should be greater than 0, current: ",
                +self.update_step,
            )
        self.pairwise_feature_extractor = PairwiseFeatureExtractor(cfg, in_channels)
        self.pooling_dim = self.pairwise_feature_extractor.pooling_dim

        self.rel_aware_on = (
            self.cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.RELATION_CONFIDENCE_AWARE
        )
        self.rel_aware_module_type = (
            self.cfg.MODEL.ROI_RELATION_HEAD.RELATION_PROPOSAL_MODEL.METHOD
        )
        self.share_parameters_each_iter = (
            cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.SHARE_PARAMETERS_EACH_ITER
        )
        param_set_num = num_iter
        if self.share_parameters_each_iter:
            param_set_num = 1

            
        self.num_rel_cls = self.cfg.MODEL.ROI_RELATION_HEAD.NUM_CLASSES

        self.relness_weighting_mp = False
        self.gating_with_relness_logits = False
        self.filter_the_mp_instance = False
        self.relation_conf_aware_models = None
        self.apply_gt_for_rel_conf = False

        self.mp_pair_refine_iter = 1

        self.graph_filtering_method = (
            cfg.MODEL.ROI_RELATION_HEAD.RELATION_PROPOSAL_MODEL.METHOD
        )

        self.vail_pair_num = cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.MP_VALID_PAIRS_NUM

        if self.rel_aware_on:

            #####  build up the relationship aware modules
            self.mp_pair_refine_iter = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.ITERATE_MP_PAIR_REFINE
            )
            assert self.mp_pair_refine_iter > 0

            self.shared_pre_rel_classifier = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.SHARE_RELATED_MODEL_ACROSS_REFINE_ITER
            )

            if self.mp_pair_refine_iter <= 1:
                self.shared_pre_rel_classifier = False

            if not self.shared_pre_rel_classifier:
                self.relation_conf_aware_models = nn.ModuleList()
                for ii in range(self.mp_pair_refine_iter):

                    if ii == 0:
                        input_dim = self.pooling_dim
                    else:
                        input_dim = self.hidden_dim
                    self.relation_conf_aware_models.append(
                        make_relation_confidence_aware_module(
                            input_dim,
                        )
                    )
                     
            else:
                input_dim = self.pooling_dim
                self.relation_conf_aware_models = make_relation_confidence_aware_module(
                    input_dim,
                )
            self.pretrain_pre_clser_mode = False

            ######  relationship confidence recalibration

            self.apply_gt_for_rel_conf = self.cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.APPLY_GT

            self.gating_with_relness_logits = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.GATING_WITH_RELNESS_LOGITS
            )
            self.relness_weighting_mp = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.RELNESS_MP_WEIGHTING
            )
            # 'minmax',  'learnable_scaling'
            self.relness_score_recalibration_method = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.RELNESS_MP_WEIGHTING_SCORE_RECALIBRATION_METHOD
            )

            if self.relness_score_recalibration_method == "learnable_scaling":
                self.learnable_relness_score_gating_recalibration = (
                    LearnableRelatednessGating()
                )
            elif self.relness_score_recalibration_method == "minmax":
                self.min_relness = nn.Parameter(
                    torch.Tensor(
                        [
                            1e-5,
                        ]
                    ),
                    requires_grad=False,
                )
                self.max_relness = nn.Parameter(
                    torch.Tensor(
                        [
                            0.5,
                        ]
                    ),
                    requires_grad=False,
                )
            # else:
            #     raise ValueError(
            #         "Invalid relness_score_recalibration_method "
            #         + self.relness_score_recalibration_method
            #     )

            self.filter_the_mp_instance = (
                cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.MP_ON_VALID_PAIRS
            )
        self.is_fully_connected = self.cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.IS_FULLY_CONNECTED
        # decrease the dimension before mp
        self.obj_downdim_fc = nn.Sequential(
            make_fc(self.pooling_dim, self.hidden_dim), # 2048
            nn.ReLU(True),
        )
        self.rel_downdim_fc = nn.Sequential(
            make_fc(self.pooling_dim, self.hidden_dim),
            nn.ReLU(True),
        )

        self.obj_pair2rel_fuse = nn.Sequential(
            nn.BatchNorm1d(self.hidden_dim * 2),
            make_fc(self.hidden_dim * 2, self.hidden_dim),
            nn.ReLU(),
        )

        self.padding_feature = nn.Parameter(
            torch.zeros((self.hidden_dim)), requires_grad=False
        )

        if use_kernel_function:
            MessagePassingUnit = MessagePassingUnit_v2
        else:
            MessagePassingUnit = MessagePassingUnit_v1



        self.gate_sub2pred = nn.Sequential(
            *[MessagePassingUnit(self.hidden_dim, gate_width) for _ in range(param_set_num)]
        )
        self.gate_obj2pred = nn.Sequential(
            *[MessagePassingUnit(self.hidden_dim, gate_width) for _ in range(param_set_num)]
        )
        self.gate_pred2sub = nn.Sequential(
            *[MessagePassingUnit(self.hidden_dim, gate_width) for _ in range(param_set_num)]
        )
        self.gate_pred2obj = nn.Sequential(
            *[MessagePassingUnit(self.hidden_dim, gate_width) for _ in range(param_set_num)]
        )

        if self.gating_with_relness_logits:
            MessagePassingUnit = MessagePassingUnitGatingWithRelnessLogits
            self.gate_pred2sub = nn.Sequential(
                *[
                    MessagePassingUnit(
                        self.hidden_dim, self.num_rel_cls, self.relness_weighting_mp
                    )
                    for _ in range(param_set_num)
                ]
            )
            self.gate_pred2obj = nn.Sequential(
                *[
                    MessagePassingUnit(
                        self.hidden_dim, self.num_rel_cls, self.relness_weighting_mp
                    )
                    for _ in range(param_set_num)
                ]
            )
        self.object_msg_fusion = nn.Sequential(
            *[MessageFusion(self.hidden_dim, dropout) for _ in range(param_set_num)]
        )  #
        self.pred_msg_fusion = nn.Sequential(
            *[MessageFusion(self.hidden_dim, dropout) for _ in range(param_set_num)]
        )

        self.output_skip_connection = (
            cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.SKIP_CONNECTION_ON_OUTPUT
        )

        self.forward_time = 0

    def set_pretrain_pre_clser_mode(self, val=True):
        self.pretrain_pre_clser_mode = val

    def normalize(self, each_img_relness, selected_rel_prop_pairs_idx):

        if len(squeeze_tensor(torch.nonzero(each_img_relness != 1.0))) > 10:
            select_relness_for_minmax = each_img_relness[selected_rel_prop_pairs_idx]
            curr_relness_max = select_relness_for_minmax.detach()[
                int(len(select_relness_for_minmax) * 0.05) :
            ].max()
            curr_relness_min = select_relness_for_minmax.detach().min()

            min_val = self.min_relness.data * 0.7 + curr_relness_min * 0.3
            max_val = self.max_relness.data * 0.7 + curr_relness_max * 0.3


            if self.training:
                # moving average for the relness scores normalization
                self.min_relness.data = self.min_relness.data * 0.9 + curr_relness_min * 0.1
                self.max_relness.data = self.max_relness.data * 0.9 + curr_relness_max * 0.1

        else:
            min_val = self.min_relness
            max_val = self.max_relness

        def minmax_norm(data, min, max):
            return (data - min) / (max - min + 1e-5)

        # apply on all non 1.0 relness scores
        each_img_relness[each_img_relness != 1.0] = torch.clamp(
            minmax_norm(each_img_relness[each_img_relness != 1.0], min_val, max_val),
            max=1.0,
            min=0.0,
        )

        return each_img_relness

    def ranking_minmax_recalibration(self, each_img_relness, selected_rel_prop_pairs_idx):

        # normalize the relness score
        each_img_relness = self.normalize(each_img_relness, selected_rel_prop_pairs_idx)

        # take the top 10% pairs set as the must keep relationship by set it relness into 1.0
        total_rel_num = len(selected_rel_prop_pairs_idx)
        each_img_relness[selected_rel_prop_pairs_idx[: int(total_rel_num * 0.1)]] += (
            1.0
            - each_img_relness[selected_rel_prop_pairs_idx[: int(total_rel_num * 0.1)]]
        )

        return each_img_relness


    def gumbel_softmax(self, each_img_relness):
        soft_sample = RelaxedBernoulli(temperature=1.0, probs=each_img_relness).rsample()
        soft_sample = clamp_probs(soft_sample)
        each_img_relness = QuantizeBernoulli.apply(soft_sample)
        return each_img_relness

    def relness_score_recalibration(self, each_img_relness, selected_rel_prop_pairs_idx):
        if self.relness_score_recalibration_method == "minmax": # Learnable_scaling
            each_img_relness = self.ranking_minmax_recalibration(
                each_img_relness, selected_rel_prop_pairs_idx
            )
        elif self.relness_score_recalibration_method == "learnable_scaling":

            each_img_relness = self.learnable_relness_score_gating_recalibration(
                each_img_relness
            )
        elif self.relness_score_recalibration_method == "gumbel":
            each_img_relness = self.gumbel_softmax(each_img_relness)
        return each_img_relness

    def _prepare_adjacency_matrix(self, proposals, rel_pair_idxs, relatedness):
        """
        prepare the index of how subject and object related to the union boxes
        :param num_proposals:
        :param rel_pair_idxs:
        :return:
            ALL RETURN THINGS ARE BATCH-WISE CONCATENATED

            rel_inds,
                extent the instances pairing matrix to the batch wised (num_rel, 2)
            subj_pred_map,
                how the instances related to the relation predicates as the subject (num_inst, rel_pair_num)
            obj_pred_map
                how the instances related to the relation predicates as the object (num_inst, rel_pair_num)
            selected_relness,
                the relatness score for selected relationship proposal that send message to adjency nodes (val_rel_pair_num, 1)
            selected_rel_prop_pairs_idx
                the relationship proposal id that selected relationship proposal that send message to adjency nodes (val_rel_pair_num, 1)
        """
        rel_inds_batch_cat = []
        offset = 0
        num_proposals = [len(props) for props in proposals]
        rel_prop_pairs_relness_batch = []

        for idx, (prop, rel_ind_i) in enumerate(
            zip(
                proposals,
                rel_pair_idxs,
            )
        ):
            rel_ind_i = rel_ind_i.long()
            if self.filter_the_mp_instance: # True
                assert relatedness is not None
                related_matrix = relatedness[idx]
                rel_prop_pairs_relness = related_matrix[rel_ind_i[:, 0], rel_ind_i[:, 1]]

                det_score = prop.get_field("pred_scores")

                rel_prop_pairs_relness_batch.append(rel_prop_pairs_relness)
            rel_ind_i = copy.deepcopy(rel_ind_i)

            rel_ind_i += offset
            offset += len(prop)
            rel_inds_batch_cat.append(rel_ind_i)
        rel_inds_batch_cat = torch.cat(rel_inds_batch_cat, 0)

        subj_pred_map = (
            rel_inds_batch_cat.new(sum(num_proposals), rel_inds_batch_cat.shape[0])
            .fill_(0)
            .float()
            .detach()
        )
        obj_pred_map = (
            rel_inds_batch_cat.new(sum(num_proposals), rel_inds_batch_cat.shape[0])
            .fill_(0)
            .float()
            .detach()
        )
        # only message passing on valid pairs

        if len(rel_prop_pairs_relness_batch) != 0:

            if self.rel_aware_on: # True
                offset = 0
                rel_prop_pairs_relness_sorted_idx = []
                rel_prop_pairs_relness_batch_update = []
                for idx, each_img_relness in enumerate(rel_prop_pairs_relness_batch):
                    (
                        selected_rel_prop_pairs_relness,
                        selected_rel_prop_pairs_idx,
                    ) = torch.sort(each_img_relness, descending=True)

                    if self.apply_gt_for_rel_conf: # False
                        # add the non-GT rel pair dynamically according to the GT rel num
                        gt_rel_idx = squeeze_tensor(
                            torch.nonzero(selected_rel_prop_pairs_relness == 1.0)
                        )
                        pred_rel_idx = squeeze_tensor(
                            torch.nonzero(selected_rel_prop_pairs_relness < 1.0)
                        )
                        pred_rel_num = int(len(gt_rel_idx) * 0.2)
                        pred_rel_num = (
                            pred_rel_num
                            if pred_rel_num < len(pred_rel_idx)
                            else len(pred_rel_idx)
                        )
                        pred_rel_num = pred_rel_num if pred_rel_num > 0 else 5
                        if self.cfg.MODEL.ROI_RELATION_HEAD.BGNN_MODULE.ADD_NON_GT:
                            selected_rel_prop_pairs_idx = torch.cat(
                                (
                                    selected_rel_prop_pairs_idx[gt_rel_idx],
                                    selected_rel_prop_pairs_idx[pred_rel_idx[:pred_rel_num]],
                                )
                            )
                        else:
                            selected_rel_prop_pairs_idx = selected_rel_prop_pairs_idx[gt_rel_idx]
                    elif self.is_fully_connected:
                        selected_rel_prop_pairs_idx = torch.arange(each_img_relness.shape[0])
                    else:
                        # recaliberating the relationship confidence for weighting
                        selected_rel_prop_pairs_idx = selected_rel_prop_pairs_idx[
                            : self.vail_pair_num
                        ]

                        if self.relness_weighting_mp and not self.pretrain_pre_clser_mode: # True, False
                            each_img_relness = self.relness_score_recalibration(
                                each_img_relness, selected_rel_prop_pairs_idx
                            )

                            selected_rel_prop_pairs_idx = squeeze_tensor(torch.nonzero(each_img_relness > 0.0001))

                    rel_prop_pairs_relness_batch_update.append(each_img_relness)

                    rel_prop_pairs_relness_sorted_idx.append(
                        selected_rel_prop_pairs_idx + offset
                    )
                    offset += len(each_img_relness)

                selected_rel_prop_pairs_idx = torch.cat(rel_prop_pairs_relness_sorted_idx, 0)
                rel_prop_pairs_relness_batch_cat = torch.cat(
                    rel_prop_pairs_relness_batch_update, 0
                )

            subj_pred_map[
                rel_inds_batch_cat[selected_rel_prop_pairs_idx, 0],
                selected_rel_prop_pairs_idx,
            ] = 1
            obj_pred_map[
                rel_inds_batch_cat[selected_rel_prop_pairs_idx, 1],
                selected_rel_prop_pairs_idx,
            ] = 1
            selected_relness = rel_prop_pairs_relness_batch_cat
        else:
            # or all relationship pairs
            selected_rel_prop_pairs_idx = torch.arange(
                len(rel_inds_batch_cat[:, 0]), device=rel_inds_batch_cat.device
            )
            selected_relness = None
            subj_pred_map.scatter_(0, (rel_inds_batch_cat[:, 0].contiguous().view(1, -1)), 1)
            obj_pred_map.scatter_(0, (rel_inds_batch_cat[:, 1].contiguous().view(1, -1)), 1)
        return (
            rel_inds_batch_cat,
            subj_pred_map,
            obj_pred_map,
            selected_relness,
            selected_rel_prop_pairs_idx,
        )

    # Here, we do all the operations out of loop, the loop is just to combine the features
    # Less kernel evoke frequency improve the speed of the model
    def prepare_message(
        self,
        target_features,
        source_features,
        select_mat,
        gate_module,
        relness_scores=None,
        relness_logits=None,
    ):
        """
        generate the message from the source nodes for the following merge operations.

        Then the message passing process can be
        :param target_features: (num_inst, dim)
        :param source_features: (num_rel, dim)
        :param select_mat:  (num_inst, rel_pair_num)
        :param gate_module:
        :param relness_scores: (num_rel, )
        :param relness_logit (num_rel, num_rel_category)

        :return: messages representation: (num_inst, dim)
        """
        feature_data = []

        if select_mat.sum() == 0:
            temp = torch.zeros(
                (target_features.size()[1:]),
                requires_grad=True,
                dtype=target_features.dtype,
                device=target_features.dtype,
            )
            feature_data = torch.stack(temp, 0)
        else:
            transfer_list = (select_mat > 0).nonzero(as_tuple=False)
            source_indices = transfer_list[:, 1] # Predicate
            target_indices = transfer_list[:, 0] # Entity
            source_f = torch.index_select(source_features, 0, source_indices)
            target_f = torch.index_select(target_features, 0, target_indices)

            select_relness = relness_scores[source_indices]

            if self.gating_with_relness_logits: # False
                assert relness_logits is not None

                # relness_dist =  relness_logits
                select_relness_dist = torch.sigmoid(relness_logits[source_indices])

                if self.relness_weighting_mp:
                    transferred_features, weighting_gate = gate_module(
                        target_f, source_f, select_relness_dist, select_relness
                    )
                else:
                    transferred_features, weighting_gate = gate_module(
                        target_f, source_f, select_relness_dist
                    )
            else:
                if self.relness_weighting_mp:
                    relness = select_relness if self.relness_score_recalibration_method != "gumbel" else None
                    select_relness = relness_scores[transfer_list[:, 1]] 
                    transferred_features, weighting_gate = gate_module( 
                        target_f, source_f, relness
                    )
                else:
                    transferred_features, weighting_gate = gate_module(target_f, source_f)
            aggregator_matrix = torch.zeros(
                (target_features.shape[0], transferred_features.shape[0]),
                dtype=weighting_gate.dtype,
                device=weighting_gate.device,
            )

            for f_id in range(target_features.shape[0]):
                if select_mat[f_id, :].data.sum() > 0:
                    # average from the multiple sources
                    feature_indices = squeeze_tensor(
                        (transfer_list[:, 0] == f_id).nonzero(as_tuple=False)
                    )  # obtain source_relevant_idx
                    # (target, source_relevant_idx)
                    aggregator_matrix[f_id, feature_indices] = 1
            # (target, source_relevant_idx) @ (source_relevant_idx, feat-dim) => (target, feat-dim)
            aggregate_feat = torch.matmul(aggregator_matrix, transferred_features)
            avg_factor = aggregator_matrix.sum(dim=1)
            vaild_aggregate_idx = avg_factor != 0
            avg_factor = avg_factor.unsqueeze(1).expand(
                avg_factor.shape[0], aggregate_feat.shape[1]
            )
            aggregate_feat[vaild_aggregate_idx] /= avg_factor[vaild_aggregate_idx].long()

            feature_data = aggregate_feat
        return feature_data


    def forward(
        self,
        inst_features,
        rel_union_features,
        proposals,
        rel_pair_inds,
        rel_gt_binarys=None,
        logger=None,
    ):
        num_inst_proposals = [len(b) for b in proposals]

        augment_obj_feat, rel_feats = self.pairwise_feature_extractor(
            inst_features,
            rel_union_features,
            proposals,
            rel_pair_inds,
        )

        relatedness_each_iters = []
        refine_rel_feats_each_iters = [rel_feats]
        refine_ent_feats_each_iters = [augment_obj_feat]
        pre_cls_logits_each_iter = []
        num_obj = [len(prop) for prop in proposals]
        for refine_iter in range(self.mp_pair_refine_iter): # 3
            pre_cls_logits = None 

            pred_relatedness_scores = None
            if self.rel_aware_on: # True
                # input_features = refine_ent_feats_each_iters[-1]

                input_features = refine_rel_feats_each_iters[-1]

                if not self.shared_pre_rel_classifier: # False
                    pre_cls_logits, pred_relatedness_scores = self.relation_conf_aware_models[
                        refine_iter
                    ](input_features, proposals, rel_pair_inds)
                else:
                    pre_cls_logits, pred_relatedness_scores = self.relation_conf_aware_models(
                        input_features, proposals, rel_pair_inds
                    )
                pre_cls_logits_each_iter.append(pre_cls_logits)
            relatedness_scores = pred_relatedness_scores

            if self.apply_gt_for_rel_conf:
                ref_relatedness = rel_gt_binarys

                if pred_relatedness_scores is None:
                    relatedness_scores = ref_relatedness
                else:
                    relatedness_scores = pred_relatedness_scores
                    for idx, ref_rel in enumerate(ref_relatedness):
                        gt_rel_idx = ref_rel.nonzero(as_tuple=False)
                        relatedness_scores[idx][gt_rel_idx[:, 0], gt_rel_idx[:, 1]] = 1.0
            elif self.is_fully_connected:
                for idx, ref_rel in enumerate(rel_gt_binarys):
                    relatedness_scores[idx] = torch.ones((ref_rel.shape[0], ref_rel.shape[0])).cuda() - torch.eye(ref_rel.shape[0]).cuda()

            relatedness_each_iters.append(relatedness_scores)

            # build up list for massage passing process
            inst_feature4iter = [
                self.obj_downdim_fc(augment_obj_feat),
            ]
            rel_feature4iter = [
                self.rel_downdim_fc(rel_feats),
            ]

            valid_inst_idx = []
            if self.filter_the_mp_instance: # True
                for p in proposals:
                    valid_inst_idx.append(p.get_field("pred_scores") > 0.03)

            if len(valid_inst_idx) > 0:
                valid_inst_idx = torch.cat(valid_inst_idx, 0)
            else:
                valid_inst_idx = torch.zeros(0)

            self.forward_time += 1

            if self.pretrain_pre_clser_mode:
                #  directly return without graph building
                refined_inst_features = inst_feature4iter[-1]
                refined_rel_features = rel_feature4iter[-1]

                refine_ent_feats_each_iters.append(refined_inst_features)
                refine_rel_feats_each_iters.append(refined_rel_features)
                continue

            else:

                (
                    batchwise_rel_pair_inds,
                    subj_pred_map,
                    obj_pred_map,
                    relness_scores,
                    selected_rel_prop_pairs_idx,
                ) = self._prepare_adjacency_matrix(
                    proposals, rel_pair_inds, relatedness_each_iters[-1]
                )

                if (
                    len(squeeze_tensor(valid_inst_idx.nonzero(as_tuple=False))) < 1
                    or len(squeeze_tensor(batchwise_rel_pair_inds.nonzero(as_tuple=False))) < 1
                    or len(squeeze_tensor(subj_pred_map.nonzero(as_tuple=False))) < 1
                    or len(squeeze_tensor(obj_pred_map.nonzero(as_tuple=False))) < 1
                    or self.pretrain_pre_clser_mode
                ):  # directly return, no mp process


                    refined_inst_features = inst_feature4iter[-1]
                    refined_rel_features = rel_feature4iter[-1]

                    refine_ent_feats_each_iters.append(refined_inst_features)
                    refine_rel_feats_each_iters.append(refined_rel_features)

                    continue

            # graph module
            for t in range(self.update_step):
                param_idx = 0
                if not self.share_parameters_each_iter:
                    param_idx = t
                """update object features pass message from the predicates to instances"""
                object_sub = self.prepare_message(
                    inst_feature4iter[t],
                    rel_feature4iter[t],
                    subj_pred_map,
                    self.gate_pred2sub[param_idx],
                    relness_scores=relness_scores,
                    relness_logits=pre_cls_logits,
                )
                object_obj = self.prepare_message( #
                    inst_feature4iter[t],
                    rel_feature4iter[t],
                    obj_pred_map,
                    self.gate_pred2obj[param_idx],
                    relness_scores=relness_scores,
                    relness_logits=pre_cls_logits,
                )

                GRU_input_feature_object = (object_sub + object_obj) / 2.0
                inst_feature4iter.append(
                    inst_feature4iter[t]
                    + self.object_msg_fusion[param_idx](
                        GRU_input_feature_object, inst_feature4iter[t]
                    )
                )

                """update predicate features from entities features"""
                indices_sub = batchwise_rel_pair_inds[:, 0]
                indices_obj = batchwise_rel_pair_inds[:, 1]  # num_rel, 1

                if self.filter_the_mp_instance: # True
                    # here we only pass massage from the fg boxes to the predicates
                    valid_sub_inst_in_pairs = valid_inst_idx[indices_sub]
                    valid_obj_inst_in_pairs = valid_inst_idx[indices_obj]
                    valid_inst_pair_inds = (valid_sub_inst_in_pairs) & (
                        valid_obj_inst_in_pairs
                    )
                    # num_rel(valid sub inst), 1 Boolean tensor
                    # num_rel(valid sub inst), 1
                    indices_sub = indices_sub[valid_inst_pair_inds]
                    # num_rel(valid obj inst), 1
                    indices_obj = indices_obj[valid_inst_pair_inds]

                    feat_sub2pred = torch.index_select(inst_feature4iter[t], 0, indices_sub)
                    feat_obj2pred = torch.index_select(inst_feature4iter[t], 0, indices_obj)
                    # num_rel(valid obj inst), hidden_dim
                    valid_pairs_rel_feats = torch.index_select(
                        rel_feature4iter[t],
                        0,
                        squeeze_tensor(valid_inst_pair_inds.nonzero(as_tuple=False)),
                    )
                    phrase_sub, sub2pred_gate_weight = self.gate_sub2pred[param_idx]( 
                        valid_pairs_rel_feats, feat_sub2pred
                    )
                    phrase_obj, obj2pred_gate_weight = self.gate_obj2pred[param_idx](
                        valid_pairs_rel_feats, feat_obj2pred
                    )
                    GRU_input_feature_phrase = (phrase_sub + phrase_obj) / 2.0
                    next_stp_rel_feature4iter = self.pred_msg_fusion[param_idx](
                        GRU_input_feature_phrase, valid_pairs_rel_feats
                    )

                    # only update valid pairs feature, others remain as initial value
                    padded_next_stp_rel_feats = rel_feature4iter[t].clone()
                    padded_next_stp_rel_feats[
                        valid_inst_pair_inds
                    ] += next_stp_rel_feature4iter

                    rel_feature4iter.append(padded_next_stp_rel_feats)
                else:

                    # obj to pred on all pairs
                    feat_sub2pred = torch.index_select(inst_feature4iter[t], 0, indices_sub)
                    feat_obj2pred = torch.index_select(inst_feature4iter[t], 0, indices_obj)
                    phrase_sub, sub2pred_gate_weight = self.gate_sub2pred[param_idx](
                        rel_feature4iter[t], feat_sub2pred
                    )
                    phrase_obj, obj2pred_gate_weight = self.gate_obj2pred[param_idx](
                        rel_feature4iter[t], feat_obj2pred
                    )
                    GRU_input_feature_phrase = (phrase_sub + phrase_obj) / 2.0
                    rel_feature4iter.append(
                        rel_feature4iter[t]
                        + self.pred_msg_fusion[param_idx](
                            GRU_input_feature_phrase, rel_feature4iter[t]
                        )
                    )
            refined_inst_features = inst_feature4iter[-1] 
            refined_rel_features = rel_feature4iter[-1]

            refine_ent_feats_each_iters.append(refined_inst_features)

            refine_rel_feats_each_iters.append(refined_rel_features)



        if len(pre_cls_logits_each_iter) == 0:
            pre_cls_logits_each_iter = None

        return (
            refine_ent_feats_each_iters[-1],
            refine_rel_feats_each_iters[-1],
            pre_cls_logits_each_iter,
            None
        )


def build_bgnn_model(cfg, in_channels):
    return BGNNContext(cfg, in_channels)
