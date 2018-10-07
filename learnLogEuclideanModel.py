# -*- coding: utf-8 -*-
"""
Log-Euclidean Bin and Delta model for the axis-angle representation
"""

import torch
from torch import nn, optim
from torch.autograd import Variable
from torch.utils.data import DataLoader

from dataGenerators import TestImages, my_collate
from binDeltaGenerators import GBDGenerator
from axisAngle import get_error2, get_R, get_y
from binDeltaModels import OneBinDeltaModel, OneDeltaPerBinModel
from helperFunctions import classes

import numpy as np
import scipy.io as spio
import math
import gc
import os
import time
import progressbar
import pickle
import argparse
from tensorboardX import SummaryWriter

parser = argparse.ArgumentParser(description='Log-Euclidean Bin & Delta Model')
parser.add_argument('--gpu_id', type=str, default='0')
parser.add_argument('--render_path', type=str, default='data/renderforcnn/')
parser.add_argument('--augmented_path', type=str, default='data/augmented2/')
parser.add_argument('--pascal3d_path', type=str, default='data/flipped_new/test')
parser.add_argument('--save_str', type=str)
parser.add_argument('--dict_size', type=int, default=200)
parser.add_argument('--num_workers', type=int, default=4)
parser.add_argument('--feature_network', type=str, default='resnet')
parser.add_argument('--N0', type=int, default=2048)
parser.add_argument('--N1', type=int, default=1000)
parser.add_argument('--N2', type=int, default=500)
parser.add_argument('--N3', type=int, default=100)
parser.add_argument('--init_lr', type=float, default=1e-4)
parser.add_argument('--num_epochs', type=int, default=3)
parser.add_argument('--max_iterations', type=float, default=np.inf)
parser.add_argument('--multires', type=bool, default=False)
args = parser.parse_args()
print(args)
# assign GPU
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id

# save stuff here
results_file = os.path.join('results', args.save_str)
model_file = os.path.join('models', args.save_str + '.tar')
plots_file = os.path.join('plots', args.save_str)
log_dir = os.path.join('logs', args.save_str)

# kmeans data
kmeans_file = 'data/kmeans_dictionary_axis_angle_' + str(args.dict_size) + '.pkl'
kmeans = pickle.load(open(kmeans_file, 'rb'))
num_clusters = kmeans.n_clusters
rotations_dict = np.stack([get_R(kmeans.cluster_centers_[i]) for i in range(kmeans.n_clusters)])

# relevant variables
ndim = 3
num_classes = len(classes)

# loss
mse_loss = nn.MSELoss().cuda()
ce_loss = nn.CrossEntropyLoss().cuda()

# DATA
# datasets
real_data = GBDGenerator(args.augmented_path, 'real', kmeans_file)
render_data = GBDGenerator(args.render_path, 'render', kmeans_file)
test_data = TestImages(args.pascal3d_path)
# setup data loaders
real_loader = DataLoader(real_data, batch_size=args.num_workers, shuffle=True, num_workers=args.num_workers, pin_memory=True, collate_fn=my_collate)
render_loader = DataLoader(render_data, batch_size=args.num_workers, shuffle=True, num_workers=args.num_workers, pin_memory=True, collate_fn=my_collate)
test_loader = DataLoader(test_data, batch_size=32, collate_fn=my_collate)
print('Real: {0} \t Render: {1} \t Test: {2}'.format(len(real_loader), len(render_loader), len(test_loader)))

if np.isinf(args.max_iterations):
	max_iterations = min(len(real_loader), len(render_loader))
else:
	max_iterations = args.max_iterations

# my_model
if not args.multires:
	model = OneBinDeltaModel(args.feature_network, num_classes, num_clusters, args.N0, args.N1, args.N2, ndim)
else:
	model = OneDeltaPerBinModel(args.feature_network, num_classes, num_clusters, args.N0, args.N1, args.N2, args.N3, ndim)

# print(model)
# loss and optimizer
optimizer = optim.Adam(model.parameters(), lr=args.init_lr)
# scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.1)
# store stuff
writer = SummaryWriter(log_dir)
count = 0
val_loss = []
s = 0


def get_residuals(ydata):
	ydata_res = np.zeros((ydata.shape[0], len(rotations_dict), 3))
	for i in range(ydata.shape[0]):
		for j in range(len(rotations_dict)):
			ydata_res[i, j, :] = get_y(np.dot(rotations_dict[j].T, get_R(ydata[i])))
	return ydata_res


# OPTIMIZATION functions
def training():
	global count, val_loss, s
	model.train()
	bar = progressbar.ProgressBar(max_value=max_iterations)
	for i, (sample_real, sample_render) in enumerate(zip(real_loader, render_loader)):
		# forward steps
		xdata_real = Variable(sample_real['xdata'].cuda())
		label_real = Variable(sample_real['label'].cuda())
		output_real = model(xdata_real, label_real)
		xdata_render = Variable(sample_render['xdata'].cuda())
		label_render = Variable(sample_render['label'].cuda())
		output_render = model(xdata_render, label_render)
		# loss
		ydata_bin = torch.cat((Variable(sample_real['ydata_bin'].cuda()), Variable(sample_render['ydata_bin'].cuda())))
		output_bin = torch.cat((output_real[0], output_render[0]))
		Lc = ce_loss(output_bin, ydata_bin)
		labels = torch.argmax(output_bin, dim=1)
		labels = torch.zeros(labels.size(0), num_clusters).scatter_(1, labels.unsqueeze(1).data.cpu(), 1.0)
		labels = Variable(labels.unsqueeze(2).cuda())
		ydata_numpy = np.concatenate((sample_real['ydata'].data.cpu().numpy(), sample_render['ydata'].data.cpu().numpy()))
		ydata_res = Variable(torch.from_numpy(get_residuals(ydata_numpy)).cuda())
		ydata_res = torch.squeeze(torch.bmm(ydata_res.permute(0, 2, 1), labels), 2)
		output_res = torch.cat((output_real[1], output_render[1]))
		Lr = mse_loss(output_res, ydata_res)
		loss = Lc + 0.5*math.exp(-2*s)*Lr + s
		# parameter updates
		optimizer.zero_grad()
		loss.backward()
		optimizer.step()
		s = 0.5*math.log(Lr)
		# store
		count += 1
		writer.add_scalar('train_loss', loss.item(), count)
		writer.add_scalar('alpha', 0.5*math.exp(-2*s), count)
		if i % 1000 == 0:
			ytest, yhat_test, test_labels = testing()
			spio.savemat(results_file, {'ytest': ytest, 'yhat_test': yhat_test, 'test_labels': test_labels})
			tmp_val_loss = get_error2(ytest, yhat_test, test_labels, num_classes)
			writer.add_scalar('val_loss', tmp_val_loss, count)
			val_loss.append(tmp_val_loss)
		# cleanup
		del xdata_real, xdata_render, label_real, label_render
		del output_bin, output_res, ydata_bin, ydata_res, labels
		del output_real, output_render, sample_real, sample_render, loss
		bar.update(i)
		# stop
		if i == max_iterations:
			break
	render_loader.dataset.shuffle_images()
	real_loader.dataset.shuffle_images()


def testing():
	model.eval()
	ypred = []
	ytrue = []
	labels = []
	for i, sample in enumerate(test_loader):
		xdata = Variable(sample['xdata'].cuda())
		label = Variable(sample['label'].cuda())
		output = model(xdata, label)
		ypred_bin = np.argmax(output[0].data.cpu().numpy(), axis=1)
		ypred_res = output[1].data.cpu().numpy()
		y = [get_y(np.dot(rotations_dict[ypred_bin[j]], get_R(ypred_res[j]))) for j in range(ypred_bin.shape[0])]
		ypred.append(y)
		ytrue.append(sample['ydata'].numpy())
		labels.append(sample['label'].numpy())
		del xdata, label, output, sample
		gc.collect()
	ypred = np.concatenate(ypred)
	ytrue = np.concatenate(ytrue)
	labels = np.concatenate(labels)
	model.train()
	return ytrue, ypred, labels


def save_checkpoint(filename):
	torch.save(model.state_dict(), filename)


# train
for epoch in range(args.num_epochs):
	tic = time.time()
	# scheduler.step()
	# training step
	training()
	# save model at end of epoch
	save_checkpoint(model_file)
	# validation
	ytest, yhat_test, test_labels = testing()
	print('\nMedErr: {0}'.format(get_error2(ytest, yhat_test, test_labels, num_classes)))
	# time and output
	toc = time.time() - tic
	print('Epoch: {0} done in time {1}s'.format(epoch, toc))
	# cleanup
	gc.collect()
writer.close()
val_loss = np.stack(val_loss)
spio.savemat(plots_file, {'val_loss': val_loss})

# evaluate the model
ytest, yhat_test, test_labels = testing()
print('\nMedErr: {0}'.format(get_error2(ytest, yhat_test, test_labels, num_classes)))
spio.savemat(results_file, {'ytest': ytest, 'yhat_test': yhat_test, 'test_labels': test_labels})