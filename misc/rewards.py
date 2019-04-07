from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
from time import time
import misc.utils as utils
from collections import OrderedDict
import torch
import torch.nn.functional as F
from torch.distributions import Dirichlet
import misc.utils as utils

import sys
sys.path.append("/home1/06008/xf993/self-critical.pytorch/cider")
sys.path.append("/home/ziyu/self-critical.pytorch/cider")
from pyciderevalcap.ciderD.ciderD import CiderD
sys.path.append("/home1/06008/xf993/self-critical.pytorch/coco-caption")
sys.path.append("/home/ziyu/self-critical.pytorch/coco-caption")
from pycocoevalcap.bleu.bleu import Bleu

CiderD_scorer = None
Bleu_scorer = None
#CiderD_scorer = CiderD(df='corpus')

def init_scorer(cached_tokens):
    global CiderD_scorer
    CiderD_scorer = CiderD_scorer or CiderD(df=cached_tokens)
    global Bleu_scorer
    Bleu_scorer = Bleu_scorer or Bleu(4)

def array_to_str(arr):
    out = ''
    for i in range(len(arr)):
        out += str(arr[i]) + ' '
        if arr[i] == 0:
            break
    return out.strip()

def get_self_critical_reward(model, fc_feats, att_feats, att_masks, data, gen_result, opt):
    batch_size = gen_result.size(0)# batch_size = sample_size * seq_per_img
    seq_per_img = batch_size // len(data['gts'])

    # get greedy decoding baseline
    model.eval()
    with torch.no_grad():
        greedy_res, _ = model(fc_feats, att_feats, att_masks=att_masks, mode='sample')
    model.train()

    res = OrderedDict()

    gen_result = gen_result.data.cpu().numpy()
    greedy_res = greedy_res.data.cpu().numpy()
    for i in range(batch_size):
        res[i] = [array_to_str(gen_result[i])]
    for i in range(batch_size):
        res[batch_size + i] = [array_to_str(greedy_res[i])]

    gts = OrderedDict()
    for i in range(len(data['gts'])):
        gts[i] = [array_to_str(data['gts'][i][j]) for j in range(len(data['gts'][i]))]

    res_ = [{'image_id':i, 'caption': res[i]} for i in range(2 * batch_size)]
    res__ = {i: res[i] for i in range(2 * batch_size)}
    gts = {i: gts[i % batch_size // seq_per_img] for i in range(2 * batch_size)}
    if opt.cider_reward_weight > 0:
        _, cider_scores = CiderD_scorer.compute_score(gts, res_)
        print('Cider scores:', _)
    else:
        cider_scores = 0
    if opt.bleu_reward_weight > 0:
        _, bleu_scores = Bleu_scorer.compute_score(gts, res__)
        bleu_scores = np.array(bleu_scores[3])
        print('Bleu scores:', _[3])
    else:
        bleu_scores = 0
    scores = opt.cider_reward_weight * cider_scores + opt.bleu_reward_weight * bleu_scores

    scores = scores[:batch_size] - scores[batch_size:]

    rewards = np.repeat(scores[:, np.newaxis], gen_result.shape[1], 1)

    return rewards


def get_reward(data, gen_result, opt, critic=False):
    batch_size = gen_result.size(0)  # batch_size = sample_size * seq_per_img
    seq_per_img = batch_size // len(data['gts'])

    res = OrderedDict()

    gen_result = gen_result.data.cpu().numpy()
    for i in range(batch_size):
        res[i] = [array_to_str(gen_result[i])]

    gts = OrderedDict()
    for i in range(len(data['gts'])):
        gts[i] = [array_to_str(data['gts'][i][j]) for j in range(len(data['gts'][i]))]

    res_ = [{'image_id': i, 'caption': res[i]} for i in range(batch_size)]
    res__ = {i: res[i] for i in range(batch_size)}
    gts = {i: gts[i % batch_size // seq_per_img] for i in range(batch_size)}
    #print(gen_result[0])
    #print(gts[0])
    if opt.cider_reward_weight > 0:
        _, cider_scores = CiderD_scorer.compute_score(gts, res_)
        # print('Cider scores:', _)
        # print('Cider scores std:', np.std(cider_scores))
    else:
        cider_scores = 0
    if opt.bleu_reward_weight > 0:
        _, bleu_scores = Bleu_scorer.compute_score(gts, res__)
        bleu_scores = np.array(bleu_scores[3])
        print('Bleu scores:', _[3])
    else:
        bleu_scores = 0
    scores = opt.cider_reward_weight * cider_scores + opt.bleu_reward_weight * bleu_scores

    rewards = np.repeat(scores[:, np.newaxis], gen_result.shape[1], 1)
    if critic:
        return rewards, np.std(cider_scores)
    if opt.rf_demean == 1:
        rewards = np.repeat(scores[:, np.newaxis] - np.mean(scores[:, np.newaxis]), gen_result.shape[1], 1)

    return rewards


def get_mct_loss(model, fc_feats, att_feats, att_masks, data, opt, loader, critic=None):
    batch_size = fc_feats.size(0)
    vocab_size = opt.vocab_size + 1
    state = model.init_hidden(batch_size)
    seq = fc_feats.new_zeros(batch_size, model.seq_length, dtype=torch.long)
    mct_baseline = fc_feats.new_zeros(batch_size, model.seq_length)
    unfinished = fc_feats.new_ones(batch_size, dtype=torch.uint8)
    temperature = getattr(opt, 'temperature', 1.0)
    seqLogprobs = fc_feats.new_zeros(batch_size, model.seq_length)
    true_length = 0
    for t in range(model.seq_length + 1):
        if t == 0:
            xt = model.img_embed(fc_feats)
        else:
            if t == 1:
                it = fc_feats.data.new(batch_size).long().zero_()
            xt = model.embed(it)

        output, state = model.core(xt, state)
        if t >= 1:
            logprobs = F.log_softmax(model.logit(output), dim=1)
            mct_baseline[:, t-1] = torch.from_numpy(complete_batch_fun(logprobs, data, seq, t, model, state, unfinished, loader,
                                                      opt, critic)).float().cuda()
            if opt.arm_sample == 'greedy':
                it = torch.max(logprobs.data, 1)[1].unsqueeze(1)
            else:
                if temperature == 1.0:
                    it = torch.multinomial(torch.exp(logprobs.data).cpu(), 1).cuda()
                    # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy) * pi, axis=1)).cuda()
                else:
                    it = torch.multinomial(torch.exp(torch.div(logprobs.data, temperature)).cpu(), 1).cuda()
                    # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy / temperature) * pi, axis=1)).cuda()
            sampleLogprobs = logprobs.gather(1, it)
            it = it.view(-1).long()

            if t == 1:
                unfinished = it > 0
            else:
                unfinished = unfinished * (it > 0)

            it = it * unfinished.type_as(it)
            seq[:, t-1] = it
            seqLogprobs[:, t - 1] = sampleLogprobs.view(-1)
            true_length += 1
            if unfinished.sum() == 0:
                break

    return seq, seqLogprobs, mct_baseline.detach()

def complete_batch_fun(logits, data, pre_seq, step, model, state, unfinished, loader, opt, critic):
    mct_sample_num = getattr(opt, 'mct_sample_num', 1)
    batch_size, _ = logits.size()
    rewards = np.zeros([batch_size, mct_sample_num])
    pseudo_actions = torch.multinomial(torch.exp(logits.data).cpu(), mct_sample_num, replacement=True).cuda()
    arm_pseudo_action_set = []
    arm_index = []
    arm_index_2 = np.zeros(0)
    arm_pseudo_counts = []
    counts_per_sample_list = []
    temperature = getattr(opt, 'temperature', 1.0)
    for i in range(batch_size):
        set_per_sample, index_per_sample, counts_per_sample = np.unique(pseudo_actions[i, :].cpu().numpy(),
                                                                        return_inverse=True, return_counts=True)
        pseudo_count = len(set_per_sample)
        arm_pseudo_counts.append(pseudo_count)
        arm_pseudo_action_set = np.concatenate([arm_pseudo_action_set, set_per_sample], axis=0)
        arm_index.append(index_per_sample)
        arm_index_2 = np.concatenate([arm_index_2, (np.ones(pseudo_count) * i)], axis=0)
        counts_per_sample_list.append(counts_per_sample)
    seqs_arm = pre_seq[arm_index_2, :]
    unfinished_arm = unfinished[arm_index_2]
    it = torch.from_numpy(arm_pseudo_action_set).long().cuda()
    seqs_arm[:, step - 1] = it * unfinished_arm.type_as(it)
    unfinished_arm = (it > 0) * unfinished_arm
    state_h, state_c = state
    state_h_arm = state_h[:, arm_index_2, :]
    state_c_arm = state_c[:, arm_index_2, :]
    state_arm = (state_h_arm, state_c_arm)
    for t in range(step + 1, model.seq_length + 1):
        if unfinished_arm.sum() == 0:
            break
        xt = model.embed(it)
        output, state_arm = model.core(xt, state_arm)
        logprobs = F.log_softmax(model.logit(output), dim=1)
        if opt.arm_sample == 'greedy':
            it = torch.max(logprobs, 1)[1]
        else:
            if temperature == 1.0:
                prob_prev = torch.exp(logprobs.data).cpu()
            else:
                prob_prev = torch.exp(torch.div(logprobs.data, temperature)).cpu()
            it = torch.multinomial(prob_prev, 1).cuda()
        it = it.view(-1).long()
        unfinished_arm = (it > 0) * unfinished_arm
        seqs_arm[:, t - 1] = it * unfinished_arm.type_as(it)
    # print('time for completion: ' + str(time() - tic))
    ## evaluate reward
    tic = time()
    seq_per_img = batch_size // len(data['gts'])
    gts = OrderedDict()
    for i in range(len(data['gts'])):
        gts[i] = [array_to_str(data['gts'][i][j]) for j in range(len(data['gts'][i]))]
    seqs_arm = seqs_arm.data.cpu().numpy()

    # print('seq arm', seqs_arm[0:arm_pseudo_counts[0]])

    if step == np.random.randint(20) and np.random.randint(20) == 1:
        sents = utils.decode_sequence(loader.get_vocab(), torch.from_numpy(seqs_arm[0:arm_pseudo_counts[0]]).cuda())
        print('imageid', data['infos'][0]['id'], '**********************At step ' + str(step))
        print('True sentence:')
        print(sents[np.argmax(counts_per_sample_list[0])])
        print('Pseudo sentences: ')
        print(sents)
        print('Pseudo action mean: ', np.mean(arm_pseudo_counts), 'std: ', np.std(arm_pseudo_counts), 'max: ',
              np.max(arm_pseudo_counts))
    res_ = []
    gts_arm = {}
    for i in range(len(arm_pseudo_action_set)):
        res_.append({'image_id': i, 'caption': [array_to_str(seqs_arm[i])]})
        i_index = arm_index_2[i]
        gts_arm[i] = gts[i_index // seq_per_img]
    # print('time for prepare reward:' + str(time() - tic))
    tic = time()
    _, arm_metric_value = CiderD_scorer.compute_score(gts_arm, res_)
    arm_index = np.array(arm_index)
    arm_index += np.repeat(np.expand_dims(np.concatenate([[0], np.cumsum(arm_pseudo_counts)[0:(batch_size - 1)]]), 1),
                           mct_sample_num, 1)
    arm_index = np.reshape(arm_index, [-1])
    # print('time for evaluating pseudo action: ' + str(time() - tic))
    # print(arm_metric_value)
    arm_metric_matrix = np.reshape(arm_metric_value[arm_index], [batch_size, mct_sample_num])
    return np.mean(arm_metric_matrix, 1)

    # seq_per_img = batch_size // len(data['gts'])
    # gts = OrderedDict()
    # for i in range(len(data['gts'])):
    #     gts[i] = [array_to_str(data['gts'][i][j]) for j in range(len(data['gts'][i]))]
    # gts_ = {}
    # for i in range(batch_size):
    #     gts_[i] = gts[i //seq_per_img]
    # temperature = getattr(opt, 'temperature', 1.0)
    # for i in range(mct_sample_num):
    #     seqs_tmp = pre_seq
    #     unfinished_tmp = unfinished
    #     if temperature == 1.0:
    #         prob_prev = torch.exp(logits.data).cpu()
    #     else:
    #         prob_prev = torch.exp(torch.div(logits.data, temperature)).cpu()
    #     it = torch.multinomial(prob_prev, 1).cuda()
    #     it = it.view(-1).long()
    #     unfinished_tmp = (it > 0) * unfinished_tmp
    #     seqs_tmp[:, step-1] = it * unfinished_tmp.type_as(it)
    #     state_tmp = state
    #     for t in range(step + 1, model.seq_length + 1):
    #         if unfinished_tmp.sum() == 0:
    #             break
    #         xt = model.embed(it)
    #         output, state_arm = model.core(xt, state_tmp)
    #         logprobs = F.log_softmax(model.logit(output), dim=1)
    #         if opt.arm_sample == 'greedy':
    #             it = torch.max(logprobs, 1)[1]
    #         else:
    #             if temperature == 1.0:
    #                 prob_prev = torch.exp(logprobs.data).cpu()
    #             else:
    #                 prob_prev = torch.exp(torch.div(logprobs.data, temperature)).cpu()
    #             it = torch.multinomial(prob_prev, 1).cuda()
    #         it = it.view(-1).long()
    #         unfinished_tmp = (it > 0) * unfinished_tmp
    #         seqs_tmp[:, t-1] = it * unfinished_tmp.type_as(it)
    #     seqs_tmp = seqs_tmp.data.cpu().numpy()
    #     if True and step == np.random.randint(20) and np.random.randint(60) == 1:
    #         sents = utils.decode_sequence(loader.get_vocab(), torch.from_numpy(seqs_tmp[0:mct_sample_num]).cuda())
    #         print('imageid', data['infos'][0]['id'], '**********************At step ' + str(step))
    #         print('Pseudo sentences: ')
    #         print(sents)
    #     res_ = []
    #     for k in range(batch_size):
    #         res_.append({'image_id': k, 'caption': [array_to_str(seqs_tmp[k])]})
    #     _,reward = CiderD_scorer.compute_score(gts_, res_)
    #     rewards[:, i] = reward
    # return np.mean(rewards, 1)



def get_arm_loss(model, fc_feats, att_feats, att_masks, data, opt, loader, critic=None):
    batch_size = fc_feats.size(0)
    vocab_size = opt.vocab_size + 1
    state = model.init_hidden(batch_size)
    seq = fc_feats.new_zeros(batch_size, model.seq_length, dtype=torch.long)
    arm_baseline = fc_feats.new_zeros(batch_size, model.seq_length)
    loss = fc_feats.new_zeros([])
    unfinished = fc_feats.new_ones(batch_size, dtype=torch.uint8)
    temperature = getattr(opt, 'temperature', 1.0)
    pseudo_action_list = fc_feats.new_ones(batch_size, model.seq_length, vocab_size, dtype=torch.long)
    seqLogprobs = fc_feats.new_zeros(batch_size, model.seq_length)
    mask_sum = 0
    true_length = 0
    pi_list = []
    logprobs_list = []
    for t in range(model.seq_length + 1):
        if t == 0:
            xt = model.img_embed(fc_feats)
        else:
            if t == 1:
                it = fc_feats.data.new(batch_size).long().zero_()
            xt = model.embed(it)

        output, state = model.core(xt, state)
        #print(opt.seq_per_img)
        if t >= 1:
            logprobs = F.log_softmax(model.logit(output), dim=1)
            pi = torch.from_numpy(np.random.dirichlet(np.ones(vocab_size), batch_size)).float().cuda()
            logprobs_demin = logprobs.data - torch.min(logprobs.data, 1)[0].unsqueeze(1).repeat(1, vocab_size)
            mask = unfinished.float()
            if opt.arm_as_baseline == 1:
                if opt.critic_model != 'att_critic_vocab' or critic == None:
                    arm_baseline[:, t-1] = arsm_f_delta_fun_batch_torch(logprobs_demin, pi, data, seq, t, model, state, unfinished, loader,
                                                     opt, critic)
                elif opt.critic_model == 'att_critic_vocab' and critic is not None:
                    pseudo_action, pi_R = arsm_f_delta_fun_batch_torch(logprobs_demin, pi, data, seq, t, model, state,
                                                                       unfinished, loader,
                                                                       opt, critic)
                    pseudo_action_list[:, t - 1, :] = pseudo_action
                    pi_list.append(pi_R)
            else:
                if opt.critic_model != 'att_critic_vocab' or critic == None :
                    f_delta = arsm_f_delta_fun_batch_torch(logprobs_demin, pi, data, seq, t, model, state, unfinished, loader,
                                                     opt, critic)
                    f_delta = f_delta / temperature
                    f_delta = (f_delta.transpose(0, 1) * mask).transpose(0, 1)
                    mask_sum += torch.sum(mask)
                    loss -= torch.sum(f_delta.detach() * logprobs)
                elif opt.critic_model == 'att_critic_vocab' and critic is not None:
                    pseudo_action, pi_R = arsm_f_delta_fun_batch_torch(logprobs_demin, pi, data, seq, t, model, state, unfinished, loader,
                                                     opt, critic)
                    pseudo_action_list[:, t - 1, :] = pseudo_action
                    pi_list.append(pi_R)
                    logprobs = logprobs / temperature
                    logprobs_list.append(logprobs)
            if opt.arm_step_sample == 'greedy':
                it = torch.max(logprobs.data, 1)[1].unsqueeze(1)
            else:
                if temperature == 1.0:
                    it = torch.multinomial(torch.exp(logprobs.data).cpu(), 1).cuda()
                    # it = torch.min(torch.log(pi) - logprobs_demin, 1)[1].unsqueeze(1)
                    # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy) * pi, axis=1)).cuda()
                else:
                    it = torch.multinomial(torch.exp(torch.div(logprobs.data, temperature)).cpu(), 1).cuda()
                    # it = torch.min(torch.log(pi) - logprobs_demin / temperature, 1)[1].unsqueeze(1)
                    # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy / temperature) * pi, axis=1)).cuda()
            sampleLogprobs = logprobs.gather(1, it)
            it = it.view(-1).long()

            if t == 1:
                unfinished = it > 0
            else:
                unfinished = unfinished * (it > 0)

            it = it * unfinished.type_as(it)
            seq[:, t-1] = it
            seqLogprobs[:, t - 1] = sampleLogprobs.view(-1)
            true_length += 1
            if unfinished.sum() == 0:
                break
    if opt.arm_as_baseline == 1 and opt.critic_model != 'att_critic_vocab' or critic is None:
        return seq, seqLogprobs, arm_baseline.detach()
    elif opt.arm_as_baseline == 1 and opt.critic_model == 'att_critic_vocab' and critic is not None:
        seq_pad = torch.cat([seq.new_zeros(seq.size(0), 1, dtype=torch.long), seq], 1)
        critic_value = critic(seq_pad, fc_feats, att_feats, True, opt, att_masks).detach()
        for t in range(true_length):
            arm_baseline[:, t] = critic_value[:, t, :].gather(1, pseudo_action_list[:, t, :]).mean(1)
        return seq, seqLogprobs, arm_baseline.detach()

    if opt.critic_model == 'att_critic_vocab' and critic is not None:
        loss = fc_feats.new_zeros([])
        seq_pad = torch.cat([seq.new_zeros(seq.size(0), 1, dtype=torch.long), seq], 1)
        critic_value = critic(seq_pad, fc_feats, att_feats, True, opt, att_masks).detach()
        mask = fc_feats.new_ones(batch_size, dtype=torch.uint8)
        for t in range(true_length):
            f_delta = critic_value[:, t, :].gather(1, pseudo_action_list[:, t, :])
            f_delta = f_delta - torch.mean(f_delta, 1).unsqueeze(1).repeat(1, vocab_size)
            f_delta = f_delta * (1.0 - vocab_size * pi_list[t]).float().unsqueeze(1).repeat(1, vocab_size)
            if t > 0:
                mask *= seq_pad[:, t] > 0
            f_delta = (f_delta.transpose(0, 1) * mask.float()).transpose(0, 1)
            mask_sum += torch.sum(mask.float())
            loss -= torch.sum(f_delta.detach() * logprobs_list[t])

    loss = loss / mask_sum
    return loss


def arsm_f_delta_fun_batch_torch(logits, pi, data, pre_seq, step, model, state, unfinished, loader, opt, critic=None, type='ars', print_pseudo=True):
    #TODO: write in torch
    batch_size, vocab_size = logits.size()
    index_batch = torch.arange(batch_size).cuda()
    index_vocab = torch.arange(vocab_size).cuda()
    temperature = getattr(opt, 'temperature', 1.0)
    if temperature == 1.0:
        exp_neg_logit = torch.exp(-logits)
        # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy) * pi, axis=1)).cuda()
    else:
        exp_neg_logit = torch.exp(-logits/temperature)
    A_cat = torch.min(pi * exp_neg_logit, 1)[1]
    if opt.ref_cat == 'random':
        R_cat = torch.randint(vocab_size, (batch_size,)).cuda()
    elif opt.ref_cat == 'action':
        R_cat = A_cat
    pseudo_actions = pseudo_action_fun(logits,  A_cat, R_cat, pi, temperature)
    pseudo_actions[(1 - unfinished), :] = A_cat[(1 - unfinished)].unsqueeze(1).repeat(1, vocab_size)
    #print('time for pseudo action: ' + str(time() - tic))
    if opt.critic_model == 'att_critic_vocab':
        return pseudo_actions, pi[index_batch, R_cat]
    tic = time()
    ## concate unique pseudo actions
    arm_pseudo_action_set = []
    arm_index = []
    arm_index_2 = np.zeros(0)
    arm_pseudo_counts = []
    counts_per_sample_list = []
    for i in range(batch_size):
        set_per_sample, index_per_sample, counts_per_sample = np.unique(pseudo_actions[i, :].cpu().numpy(), return_inverse=True, return_counts=True)
        pseudo_count = len(set_per_sample)
        arm_pseudo_counts.append(pseudo_count)
        arm_pseudo_action_set = np.concatenate([arm_pseudo_action_set, set_per_sample], axis=0)
        arm_index.append(index_per_sample)
        arm_index_2 = np.concatenate([arm_index_2, (np.ones(pseudo_count) * i)], axis=0)
        counts_per_sample_list.append(counts_per_sample)
    ## complete sentences
    tic= time()
    if np.sum(arm_pseudo_counts) == batch_size:
        if opt.arm_as_baseline == 1:
            return torch.from_numpy(np.ones([batch_size]) * -1).float().cuda()
        else:
            return torch.from_numpy(np.zeros([batch_size, vocab_size])).float().cuda()
    seqs_arm = pre_seq[arm_index_2, :]
    unfinished_arm = unfinished[arm_index_2]
    it = torch.from_numpy(arm_pseudo_action_set).long().cuda()
    seqs_arm[:, step-1] = it * unfinished_arm.type_as(it)
    unfinished_arm = (it > 0) * unfinished_arm
    state_h, state_c = state
    state_h_arm = state_h[:, arm_index_2, :]
    state_c_arm = state_c[:, arm_index_2, :]
    state_arm = (state_h_arm, state_c_arm)
    if critic == None:
        for t in range(step + 1, model.seq_length + 1):
            if unfinished_arm.sum() == 0:
                break
            xt = model.embed(it)
            output, state_arm = model.core(xt, state_arm)
            logprobs = F.log_softmax(model.logit(output), dim=1)
            if opt.arm_sample == 'greedy':
                it = torch.max(logprobs, 1)[1]
            else:
                if temperature == 1.0:
                    prob_prev = torch.exp(logprobs.data).cpu()
                else:
                    prob_prev = torch.exp(torch.div(logprobs.data, temperature)).cpu()
                it = torch.multinomial(prob_prev, 1).cuda()
            it = it.view(-1).long()
            unfinished_arm = (it > 0) * unfinished_arm
            seqs_arm[:, t-1] = it * unfinished_arm.type_as(it)
        #print('time for completion: ' + str(time() - tic))

        ## evaluate reward
        tic = time()
        seq_per_img = batch_size // len(data['gts'])
        gts = OrderedDict()
        for i in range(len(data['gts'])):
            gts[i] = [array_to_str(data['gts'][i][j]) for j in range(len(data['gts'][i]))]
        seqs_arm = seqs_arm.data.cpu().numpy()

        #print('seq arm', seqs_arm[0:arm_pseudo_counts[0]])

        if print_pseudo and step == np.random.randint(20) and np.random.randint(20) == 1:
            sents = utils.decode_sequence(loader.get_vocab(), torch.from_numpy(seqs_arm[0:arm_pseudo_counts[0]]).cuda())
            print('imageid', data['infos'][0]['id'], '**********************At step ' + str(step))
            print('True sentence:' )
            print(sents[np.argmax(counts_per_sample_list[0])])
            print('Pseudo sentences: ')
            print(sents)
            print('Pseudo action mean: ', np.mean(arm_pseudo_counts), 'std: ', np.std(arm_pseudo_counts), 'max: ', np.max(arm_pseudo_counts))
        res_ = []
        gts_arm = {}
        cum_count = np.cumsum(arm_pseudo_counts)
        for i in range(len(arm_pseudo_action_set)):
            res_.append({'image_id': i, 'caption': [array_to_str(seqs_arm[i])]})
            i_index = arm_index_2[i]
            gts_arm[i] = gts[i_index // seq_per_img]
        #print('time for prepare reward:' + str(time() - tic))
        tic = time()
        _, arm_metric_value = CiderD_scorer.compute_score(gts_arm, res_)
    else:
        if opt.critic_model == 'state_critic':
            xt = model.embed(it)
            output, state_arm = model.core(xt, state_arm)
            arm_metric_value = critic.core(state_arm).detach().cpu().numpy()
    arm_index = np.array(arm_index)
    arm_index += np.repeat(np.expand_dims(np.concatenate([[0], np.cumsum(arm_pseudo_counts)[0:(batch_size-1)]]), 1), vocab_size, 1)
    arm_index = np.reshape(arm_index, [-1])
    #print('time for evaluating pseudo action: ' + str(time() - tic))
    #print(arm_metric_value)
    arm_metric_matrix = np.reshape(arm_metric_value[arm_index], [batch_size, vocab_size])
    if opt.arm_as_baseline == 1:
        return torch.from_numpy(arm_metric_matrix).float().cuda().mean(1)
    f_delta = arm_metric_matrix - np.repeat(np.expand_dims(np.mean(arm_metric_matrix, 1), 1), vocab_size, 1)
    f_delta = f_delta * np.repeat(np.expand_dims(1.0 - vocab_size * pi[index_batch, R_cat].cpu().numpy(), 1), vocab_size, 1)
    return torch.from_numpy(f_delta).float().cuda()

def pseudo_action_fun(logits, A_cat, R_cat, pi, temperature=1):
    batch_size, vocab_size = logits.size()
    index_batch = torch.arange(batch_size).cuda()
    index_vocab = torch.arange(vocab_size).cuda()
    if temperature == 1.0:
        exp_neg_logit = torch.exp(-logits)
        # it = torch.from_numpy(np.argmin(np.exp(-logprobs_numpy) * pi, axis=1)).cuda()
    else:
        exp_neg_logit = torch.exp(-logits/temperature)
    pseudo_actions = A_cat.unsqueeze(1).repeat(1, vocab_size)
    pseudo_actions += (exp_neg_logit * pi[index_batch, R_cat].unsqueeze(1).repeat(1, vocab_size) < min_value).long() * \
                      (index_vocab - A_cat.unsqueeze(1))
    pseudo_actions += (pi * exp_neg_logit[index_batch, R_cat].unsqueeze(1).repeat(1, vocab_size) < min_value).long() * \
                      (R_cat - A_cat).unsqueeze(1).repeat(1, vocab_size)
    index_matrix = torch.zeros_like(logits).long()
    index_matrix[index_batch, A_cat] = 1
    index_matrix[R_cat == A_cat, :] = 1

    topk, indices = torch.topk(-pi * exp_neg_logit, 2, dim=1)
    top_2_indices = indices[:, 1]
    top_2_values = -topk[:, 1].unsqueeze(1).repeat(1, vocab_size)
    candidate_i_value = exp_neg_logit * pi[index_batch, R_cat].unsqueeze(1).repeat(1, vocab_size)
    candidate_A_value = pi * exp_neg_logit[index_batch, R_cat].unsqueeze(1).repeat(1, vocab_size)
    pseudo_actions_true = top_2_indices.unsqueeze(1).repeat(1, vocab_size)
    pseudo_actions_true += (candidate_i_value < top_2_values).long() * (candidate_i_value <= candidate_A_value).long() * \
                           (index_vocab - top_2_indices.unsqueeze(1))
    pseudo_actions_true += (candidate_A_value < top_2_values).long() * (candidate_A_value < candidate_i_value).long() * \
                           (R_cat - top_2_indices).unsqueeze(1).repeat(1, vocab_size)

    pseudo_actions = pseudo_actions + index_matrix * (pseudo_actions_true - pseudo_actions)
    return pseudo_actions
