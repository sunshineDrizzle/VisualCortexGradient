import os
import time
import numpy as np
import pandas as pd
import pickle as pkl
import nibabel as nib
from os.path import join as pjoin
from scipy.stats import zscore
from scipy.spatial.distance import cdist
from sklearn.decomposition import PCA
from magicbox.io.io import CiftiReader, save2cifti
from magicbox.stats.outlier import outlier_iqr
from cxy_visual_dev.lib.predefine import All_count_32k, proj_dir,\
    Atlas, s1200_avg_eccentricity,\
    get_rois, LR_count_32k, mmp_map_file, s1200_group_rsfc_mat
from cxy_visual_dev.lib.algo import decompose, AgeSlideWindow

anal_dir = pjoin(proj_dir, 'analysis')
work_dir = pjoin(anal_dir, 'decomposition')
if not os.path.isdir(work_dir):
    os.makedirs(work_dir)


def pca_csv(
    fpath, out_csv1, out_csv2, out_pkl, columns=None, row_mask=None,
    zscore0=False, n_component=None, random_state=None
):
    """
    Args:
        fpath (str): a CSV file
        out_csv1 (str): a CSV file
            shape=(n_row, n_component)
        out_csv2 (str): a CSV file
            shape=(n_component, n_col)
        out_pkl (str): a pkl file
            fitted model
        columns (list, optional): a list of column names
            If None, use all columns
        row_mask (ndarray, optional): 1D index array
            If None, use all rows
        zscore0 (bool, optional): Default is False
            If True, do zscore for each column
        n_component (int, optional): the number of components
        random_state (int, optional):
    """
    # prepare
    df = pd.read_csv(fpath)

    if columns is None:
        data = np.array(df)
        columns = df.columns
    else:
        data = np.array(df[columns])
    n_row, n_col = data.shape

    if row_mask is not None:
        data = data[row_mask]

    if zscore0:
        data = zscore(data, 0)

    # calculate
    transformer = PCA(n_components=n_component, random_state=random_state)
    transformer.fit(data)
    Y = transformer.transform(data)
    csv_data1 = Y
    csv_data2 = transformer.components_

    # save
    if n_component is None:
        n_component = csv_data1.shape[1]
    else:
        assert n_component == csv_data1.shape[1]
    component_names = [f'C{i}' for i in range(1, n_component+1)]

    if row_mask is not None:
        csv_data1_tmp = np.ones((n_row, n_component), np.float64) * np.nan
        csv_data1_tmp[row_mask] = csv_data1
        csv_data1 = csv_data1_tmp
    out_df1 = pd.DataFrame(data=csv_data1, columns=component_names)
    out_df1.to_csv(out_csv1, index=False)

    out_df2 = pd.DataFrame(data=csv_data2, columns=columns, index=component_names)
    out_df2.to_csv(out_csv2, index=True)

    pkl.dump(transformer, open(out_pkl, 'wb'))


def csv2cifti(src_file, rois, atlas_name, out_file):
    """
    pca_csv的后续
    把ROI的权重存成cifti格式 方便可视化在大脑上
    """
    # prepare
    df = pd.read_csv(src_file, index_col=0)
    atlas = Atlas(atlas_name)
    assert atlas.maps.shape == (1, LR_count_32k)
    out_data = np.ones((df.shape[0], LR_count_32k), np.float64) * np.nan

    if rois == 'all':
        rois = df.columns

    # calculate
    for roi in rois:
        mask = atlas.maps[0] == atlas.roi2label[roi]
        for i, idx in enumerate(df.index):
            out_data[i, mask] = df.loc[idx, roi]

    # save
    save2cifti(out_file, out_data, CiftiReader(mmp_map_file).brain_models(),
               df.index.to_list())


def pca_rsfc(
    fpath, out_comp_file, out_weight_file, out_model_file,
    zscore0=False, n_component=None, random_state=None
):
    """
    Args:
        fpath (str): a pickle file
        out_comp_file (str): a .dscalar.nii file
            shape=(n_component, LR_count_32k/All_count_32k)
        out_weight_file (str): a .dscalar.nii file
            shape=(n_component, LR_count_32k/All_count_32k)
        out_model_file (str): a pkl file
            fitted model
        zscore0 (bool, optional): Default is False
            If True, do zscore for each column
        n_component (int, optional): the number of components
        random_state (int, optional):
    """
    # prepare
    data = pkl.load(open(fpath, 'rb'))
    X = data['matrix']
    n_vtx = X.shape[1]
    if n_vtx == LR_count_32k:
        reader = CiftiReader(mmp_map_file)
    elif n_vtx == All_count_32k:
        reader = CiftiReader(s1200_avg_eccentricity)
    else:
        raise ValueError("not supported n_vtx:", n_vtx)
    bms = reader.brain_models()
    vol = reader.volume

    if zscore0:
        X = zscore(X, 0)

    # calculate
    transformer = PCA(n_components=n_component, random_state=random_state)
    transformer.fit(X)
    Y = transformer.transform(X)
    if n_component is None:
        n_component = Y.shape[1]
    else:
        assert n_component == Y.shape[1]
    component_names = [f'C{i}' for i in range(1, n_component+1)]

    out_data1 = np.ones((n_component, n_vtx), np.float64) * np.nan
    out_data1[:, data['row-idx_to_32k-fs-LR-idx']] = Y.T
    out_data2 = transformer.components_

    # save
    save2cifti(out_comp_file, out_data1, bms, component_names, vol)
    save2cifti(out_weight_file, out_data2, bms, component_names, vol)
    pkl.dump(transformer, open(out_model_file, 'wb'))


def pca_HCPY_avg_rsfc_mat(mask_name0, mask_name1, dtype='z',
                          zscore0=False, n_component=None, random_state=None):
    """
    需要占用大量内存（33G以上），我是在hebb上跑的。
    如果mask_name0和mask_name1都是grayordinate，大约需要占用90G
    用PCA对HCPYA发布的组RSFC矩阵(91282, 91282)进行降维

    Args:
        mask_name0 (str): 指定使用哪些行
            'grayordinate': 所有行
            'MMP-vis3-R': 第3版右侧视觉皮层
        mask_name1 (str): 指定使用哪些列
            'grayordinate': 所有列
        dtype (str):
            'z': 就用官方发布的fisherZ转换后的数据
            'r': 用自行将z值转回r值之后的数据
        zscore0 (bool, optional): Default is False
            If True, do zscore for each column
        n_component (int, optional): the number of components
        random_state (int, optional):
    """
    fname = f'S1200-grp-RSFC-{dtype}_{mask_name0}2{mask_name1}'
    if dtype == 'z':
        src_file = s1200_group_rsfc_mat
    elif dtype == 'r':
        src_file = pjoin(proj_dir, 'data/HCP/S1200_1003_rfMRI_MSMAll_groupPCA_d4500ROW_corr.dscalar.nii')
    else:
        raise ValueError('not supported dtype:', dtype)

    # get RSFC matrix
    reader = CiftiReader(src_file)
    bms = reader.brain_models()
    vol = reader.volume
    X = reader.get_data()

    # get rows
    if mask_name0 == 'grayordinate':
        mask0 = None
    elif mask_name0 == 'cortex':
        mask0 = Atlas('HCP-MMP').get_mask('LR', 'grayordinate')[0]
        X = X[mask0]
    elif mask_name0 == 'MMP-vis3-R':
        mask0 = Atlas('HCP-MMP').get_mask(get_rois(mask_name0), 'grayordinate')[0]
        X = X[mask0]
    elif mask_name0 == 'MMP-vis3-L':
        mask0 = Atlas('HCP-MMP').get_mask(get_rois(mask_name0), 'grayordinate')[0]
        X = X[mask0]
    else:
        raise ValueError('not supported mask_name0:', mask_name0)

    # get columns
    if mask_name1 == 'grayordinate':
        mask1 = None
    elif mask_name1 == 'cortex':
        mask1 = Atlas('HCP-MMP').get_mask('LR', 'grayordinate')[0]
        X = X[:, mask1]
    elif mask_name1 == 'MMP-vis3-R':
        mask1 = Atlas('HCP-MMP').get_mask(get_rois(mask_name1), 'grayordinate')[0]
        X = X[:, mask1]
    elif mask_name1 == 'MMP-vis3-L':
        mask1 = Atlas('HCP-MMP').get_mask(get_rois(mask_name1), 'grayordinate')[0]
        X = X[:, mask1]
    else:
        raise ValueError('not supported mask_name1:', mask_name1)

    # zscore
    if zscore0:
        fname = fname + '_zscore'
        X = zscore(X, 0)

    # calculate
    print('X.shape:', X.shape)
    transformer = PCA(n_components=n_component, random_state=random_state)
    transformer.fit(X)
    Y = transformer.transform(X)
    if n_component is None:
        n_component = Y.shape[1]
    else:
        assert n_component == Y.shape[1]
    component_names = [f'C{i}' for i in range(1, n_component+1)]

    if mask0 is None:
        out_data1 = Y.T
    else:
        out_data1 = np.ones((n_component, All_count_32k), np.float64) * np.nan
        out_data1[:, mask0] = Y.T
    
    if mask1 is None:
        out_data2 = transformer.components_
    else:
        out_data2 = np.ones((n_component, All_count_32k), np.float64) * np.nan
        out_data2[:, mask1] = transformer.components_

    # save
    out_comp_file = pjoin(work_dir, f'{fname}_PCA-comp.dscalar.nii')
    out_weight_file = pjoin(work_dir, f'{fname}_PCA-weight.dscalar.nii')
    out_model_file = pjoin(work_dir, f'{fname}_PCA.pkl')
    save2cifti(out_comp_file, out_data1, bms, component_names, vol)
    save2cifti(out_weight_file, out_data2, bms, component_names, vol)
    pkl.dump(transformer, open(out_model_file, 'wb'))


def pca_HCP_MT_SW(dataset_name, vis_name, width, step,
                  merge_remainder, n_component, random_state):
    """
    用每个滑窗(slide window)内的所有被试的myelin和thickness map做联合空间PCA
    """
    m_file = pjoin(proj_dir, f'data/HCP/{dataset_name}_myelin.dscalar.nii')
    t_file = pjoin(proj_dir, f'data/HCP/{dataset_name}_corrThickness.dscalar.nii')
    mask = Atlas('HCP-MMP').get_mask(get_rois(vis_name))[0]
    asw = AgeSlideWindow(dataset_name, width, step, merge_remainder)
    out_name = f'{dataset_name}-M+corrT_{vis_name}_zscore1_PCA-subj_SW-width{width}-step{step}'
    if asw.merge_remainder:
        out_name += '-merge'
    out_file = pjoin(work_dir, f'{out_name}.pkl')

    m_maps = nib.load(m_file).get_fdata()[:, mask]
    t_maps = nib.load(t_file).get_fdata()[:, mask]
    out_dict = {'n_win': asw.n_win, 'age in years': np.zeros(asw.n_win),
                'component name': [f'C{i}' for i in range(1, n_component+1)],
                '32k_LR_mask': mask}
    for win_idx in range(asw.n_win):
        time1 = time.time()
        win_id = win_idx + 1
        indices = asw.get_subj_indices(win_id)
        n_idx = len(indices)
        X = np.r_[m_maps[indices], t_maps[indices]].T
        X = zscore(X, 0)
        transformer = PCA(n_components=n_component, random_state=random_state)
        transformer.fit(X)
        Y = transformer.transform(X)
        assert n_component == Y.shape[1]
        out_dict[f'Win{win_id}_comp'] = Y.T
        out_dict[f'Win{win_id}_weight_Myelination'] = transformer.components_[:, :n_idx]
        out_dict[f'Win{win_id}_weight_Thickness'] = transformer.components_[:, n_idx:]
        out_dict[f'Win{win_id}_model'] = transformer
        out_dict['age in years'][win_idx] = asw.get_ages(win_id, 'year').mean()
        print(f'Finished Win-{win_id}/{asw.n_win}, cost: {time.time() - time1} seconds.')

    pkl.dump(out_dict, open(out_file, 'wb'))


def pca_HCP_MT_SW_param(src_file, pc_names, out_file):
    """
    获取每个年龄窗口指定成分的各种参数
    """
    data = pkl.load(open(src_file, 'rb'))
    out_data = {'n_win': data['n_win'], 'age in years': data['age in years']}
    for pc_name in pc_names:
        pc_idx = data['component name'].index(pc_name)
        out_data[f'{pc_name} explanation ratio'] = np.zeros(data['n_win'])
        out_data[f'{pc_name} gradient range'] = np.zeros(data['n_win'])
        out_data[f'{pc_name} gradient variation'] = np.zeros(data['n_win'])
        for win_idx in range(data['n_win']):
            win_id = win_idx + 1
            pc_map = data[f'Win{win_id}_comp'][pc_idx]
            out_data[f'{pc_name} explanation ratio'][win_idx] = \
                data[f'Win{win_id}_model'].explained_variance_ratio_[pc_idx]
            out_data[f'{pc_name} gradient range'][win_idx] = \
                pc_map.max() - pc_map.min()
            out_data[f'{pc_name} gradient variation'][win_idx] = pc_map.std(ddof=1)

    out_data['gradient dispersion'] = np.zeros(data['n_win'])
    pc_indices = [data['component name'].index(i) for i in pc_names]
    for win_idx in range(data['n_win']):
        win_id = win_idx + 1
        pc_maps = data[f'Win{win_id}_comp'][pc_indices].T
        centroid = np.mean(pc_maps, 0, keepdims=True)
        out_data['gradient dispersion'][win_idx] = \
            np.sum(cdist(centroid, pc_maps, 'euclidean')[0] ** 2)

    pkl.dump(out_data, open(out_file, 'wb'))


def pca_HCPDA_MT_SW_param_local(src_file, pc_names, local_name, out_file):
    """
    获取每个年龄窗口指定成分各局部的各种参数
    """
    if local_name == 'MMP-vis3-R-EDMV':
        reader = CiftiReader(pjoin(anal_dir, 'tmp/MMP-vis3-EDMV.dlabel.nii'))
        vis_mask = Atlas('HCP-MMP').get_mask(get_rois('MMP-vis3-R'))[0]
        mask = reader.get_data()[0, vis_mask]
        local_name2key = {}
        lbl_tab = reader.label_tables()[0]
        for k in lbl_tab.keys():
            if k == 0:
                continue
            local_name2key[lbl_tab[k].label] = k
    else:
        raise ValueError('not supported local name:', local_name)

    data = pkl.load(open(src_file, 'rb'))
    out_data = {'n_win': data['n_win'], 'age in months': data['age in months']}
    for local_name, local_key in local_name2key.items():
        mask_local = mask == local_key
        out_data[local_name] = {}
        for pc_name in pc_names:
            pc_idx = data['component name'].index(pc_name)
            out_data[local_name][f'{pc_name} gradient range'] = np.zeros(data['n_win'])
            out_data[local_name][f'{pc_name} gradient variation'] = np.zeros(data['n_win'])
            for win_idx in range(data['n_win']):
                win_id = win_idx + 1
                pc_map = data[f'Win{win_id}_comp'][pc_idx][mask_local]
                out_data[local_name][f'{pc_name} gradient range'][win_idx] = \
                    pc_map.max() - pc_map.min()
                out_data[local_name][f'{pc_name} gradient variation'][win_idx] = \
                    pc_map.std()

        out_data[local_name]['gradient dispersion'] = np.zeros(data['n_win'])
        pc_indices = [data['component name'].index(i) for i in pc_names]
        for win_idx in range(data['n_win']):
            win_id = win_idx + 1
            pc_maps = data[f'Win{win_id}_comp'][pc_indices][:, mask_local].T
            centroid = np.mean(pc_maps, 0, keepdims=True)
            out_data[local_name]['gradient dispersion'][win_idx] = \
                np.sum(cdist(centroid, pc_maps, 'euclidean')[0] ** 2)
    
    pkl.dump(out_data, open(out_file, 'wb'))


def get_MT_PC_individual():
    """
    在被试内拼接myelin和thickness (or corrThickness)进行空间PCA得到两个成分
    得到：
    1. HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC1.dscalar.nii
        保存着所有被试的第1个成分
    2. HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC2.dscalar.nii
        保存着所有被试的第2个成分
    3. HCPY-1069_MMP-vis3-R_M+corrT-zscore_PCA.csv
        保存在所有被试中，各成分的方差占比率，以及M和T对各成分的贡献
    """
    m_file = pjoin(proj_dir, 'data/HCP/HCPY_myelin.dscalar.nii')
    t_file = pjoin(proj_dir, 'data/HCP/HCPY_corrThickness.dscalar.nii')
    mask = Atlas('HCP-MMP').get_mask(get_rois('MMP-vis3-R'))[0]
    fname = 'HCPY-1069_MMP-vis3-R_M+corrT-zscore'
    out_file1 = pjoin(work_dir, f'{fname}_PC1.dscalar.nii')
    out_file2 = pjoin(work_dir, f'{fname}_PC2.dscalar.nii')
    out_file3 = pjoin(work_dir, f'{fname}_PCA.csv')

    reader = CiftiReader(m_file)
    n_map, n_vtx = reader.full_data.shape
    bms = reader.brain_models()
    vol = reader.volume
    map_names = reader.map_names()
    m_maps = reader.get_data()[:, mask]
    t_maps = nib.load(t_file).get_fdata()[:, mask]
    m_maps = zscore(m_maps, 1)
    t_maps = zscore(t_maps, 1)

    c1_maps = np.ones((n_map, n_vtx)) * np.nan
    c2_maps = np.ones((n_map, n_vtx)) * np.nan
    csv_dict = {
        'C1_explained_variance_ratio': np.zeros(n_map),
        'C1_weight_M': np.zeros(n_map),
        'C1_weight_T': np.zeros(n_map),
        'C2_explained_variance_ratio': np.zeros(n_map),
        'C2_weight_M': np.zeros(n_map),
        'C2_weight_T': np.zeros(n_map)}
    for map_idx in range(n_map):
        pca = PCA(random_state=7)
        X = np.array([m_maps[map_idx], t_maps[map_idx]]).T
        pca.fit(X)
        Y = pca.transform(X)

        c1_maps[map_idx][mask] = Y[:, 0]
        c2_maps[map_idx][mask] = Y[:, 1]
        csv_dict['C1_explained_variance_ratio'][map_idx] = \
            pca.explained_variance_ratio_[0]
        csv_dict['C2_explained_variance_ratio'][map_idx] = \
            pca.explained_variance_ratio_[1]
        csv_dict['C1_weight_M'][map_idx] = pca.components_[0, 0]
        csv_dict['C1_weight_T'][map_idx] = pca.components_[0, 1]
        csv_dict['C2_weight_M'][map_idx] = pca.components_[1, 0]
        csv_dict['C2_weight_T'][map_idx] = pca.components_[1, 1]
        print(f'Finished: {map_idx+1}/{n_map}')

    save2cifti(out_file1, c1_maps, bms, map_names, vol)
    save2cifti(out_file2, c2_maps, bms, map_names, vol)
    pd.DataFrame(csv_dict).to_csv(out_file3, index=False)


def beh_data_for_PCA():
    """
    目的是先对选中的N个行为指标进行PCA提取不同含义的成分
    然后计算主梯度或次梯度的M和T权重对这些成分的拟合得分
    这里就是为此目的准备数据，先去掉缺失任一指标的被试，
    然后去掉在任一指标中属于异常值的被试。
    """
    # settings
    iqr_coef = 2  # None, 1.5, ...
    Hemis = ('L', 'R')
    pc_names = ('C1', 'C2')
    meas_names = ('M', 'T')
    beh_ver = 'v1'
    beh_cols = [
        'PicSeq_Unadj', 'CardSort_Unadj', 'Flanker_Unadj',
        'PMAT24_A_CR', 'ReadEng_Unadj', 'PicVocab_Unadj',
        'ProcSpeed_Unadj', 'DDisc_AUC_200', 'DDisc_AUC_40K',
        'VSPLOT_TC', 'SCPT_SEN', 'SCPT_SPEC', 'IWRD_TOT',
        'ListSort_Unadj', 'Noise_Comp', 'Odor_Unadj',
        'Taste_Unadj', 'EVA_Denom', 'Mars_Final']
    if iqr_coef is None:
        out_file = pjoin(work_dir, f'beh_data_for_PCA-{beh_ver}.pkl')
    else:
        out_file = pjoin(work_dir, f'beh_data_for_PCA-{beh_ver}_IQR{iqr_coef}.pkl')

    # prepare files
    meas2file = {
        'M': pjoin(
            work_dir,
            'HCPY-M+corrT_MMP-vis3-{Hemi}_zscore1_PCA-subj_M.csv'),
        'T': pjoin(
            work_dir,
            'HCPY-M+corrT_MMP-vis3-{Hemi}_zscore1_PCA-subj_corrT.csv')}
    beh_file1 = '/nfs/z1/HCP/HCPYA/S1200_behavior.csv'
    beh_file2 = '/nfs/z1/HCP/HCPYA/S1200_behavior_restricted.csv'
    info_file = pjoin(proj_dir, 'data/HCP/HCPY_SubjInfo.csv')

    # loading
    weight_names = []
    weight_arr = []
    for Hemi in Hemis:
        for meas_name in meas_names:
            weight_file = meas2file[meas_name].format(Hemi=Hemi)
            weight_df = pd.read_csv(weight_file, usecols=pc_names)
            weight_arr.append(weight_df.values)
            weight_names.extend([f'{Hemi}_{i}_{meas_name}_weight' for i in pc_names])
    weight_arr = np.concatenate(weight_arr, axis=1)
    beh_df1 = pd.read_csv(beh_file1, index_col='Subject')
    beh_df2 = pd.read_csv(beh_file2, index_col='Subject')
    assert np.all(beh_df1.index == beh_df2.index)
    beh_df = pd.concat([beh_df1, beh_df2], axis=1)
    info_df = pd.read_csv(info_file, index_col='subID')
    beh_df = beh_df.loc[info_df.index, beh_cols]
    beh_arr = beh_df.values

    # processing
    non_nan_vec = ~np.any(np.isnan(beh_arr), 1)
    weight_arr = weight_arr[non_nan_vec]
    beh_arr = beh_arr[non_nan_vec]
    if iqr_coef is not None:
        outlier_mask1 = outlier_iqr(weight_arr, iqr_coef, 0)
        outlier_mask2 = outlier_iqr(beh_arr, iqr_coef, 0)
        outlier_mask1 = np.any(outlier_mask1, 1)
        outlier_mask2 = np.any(outlier_mask2, 1)
        outlier_mask = np.logical_or(outlier_mask1, outlier_mask2)
        mask = ~outlier_mask
        weight_arr = weight_arr[mask]
        beh_arr = beh_arr[mask]
    print('weight names:', weight_names)
    print('weight_arr.shape:', weight_arr.shape)
    print('behavior names:', beh_cols)
    print('beh_arr.shape:', beh_arr.shape)

    # save out
    out_dict = {
        'weight_name': weight_names, 'weight_arr': weight_arr,
        'beh_name': beh_cols, 'beh_arr': beh_arr}
    pkl.dump(out_dict, open(out_file, 'wb'))


def pca_beh_data():

    postfix = 'v1_IQR2'
    data_file = pjoin(work_dir, f'beh_data_for_PCA-{postfix}.pkl')
    out_file = pjoin(work_dir, f'PCA_beh_data_{postfix}.pkl')

    # loading
    data = pkl.load(open(data_file, 'rb'))
    beh_arr = data['beh_arr']
    beh_arr = zscore(beh_arr, 0)

    # calculate
    pca = PCA(random_state=7)
    pca.fit(beh_arr)
    pc_arr = pca.transform(beh_arr).T
    n_component = pc_arr.shape[0]
    component_names = [f'C{i}' for i in range(1, n_component+1)]

    # save
    data['PCA'] = {}
    data['PCA']['component_name'] = component_names
    data['PCA']['arr_shape'] = '(n_component, ?)'
    data['PCA']['var_ratio'] = pca.explained_variance_ratio_
    data['PCA']['pc_arr'] = pc_arr
    data['PCA']['beh_weight'] = pca.components_
    data['arr_shape'] = '(n_subj, ?)'
    pkl.dump(data, open(out_file, 'wb'))


if __name__ == '__main__':
    # 在成人数据上，分别对左右脑HCP-MMP1_visual-cortex3做zscore
    # 联合myelin和corrThickness做空间PCA
    # vis_mask = 'MMP-vis3-L'  # MMP-vis3-L, MMP-vis3-R
    # decompose(
    #     fpaths=[
    #         pjoin(proj_dir, 'data/HCP/HCPY_myelin.dscalar.nii'),
    #         pjoin(proj_dir, 'data/HCP/HCPY_corrThickness.dscalar.nii')],
    #     cat_shape=(2, 1), method='PCA', axis=0,
    #     csv_files=[
    #         pjoin(work_dir, f'HCPY-M+corrT_{vis_mask}_zscore1_PCA-subj_M.csv'),
    #         pjoin(work_dir, f'HCPY-M+corrT_{vis_mask}_zscore1_PCA-subj_corrT.csv')],
    #     cii_files=[pjoin(work_dir, f'HCPY-M+corrT_{vis_mask}_zscore1_PCA-subj.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'HCPY-M+corrT_{vis_mask}_zscore1_PCA-subj.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois(vis_mask))[0]],
    #     map_mask=None, zscore0=None, zscore1='split', n_component=20, random_state=7
    # )

    # 在成人数据上，对左脑或右脑HCP-MMP1_visual-cortex3做zscore
    # 对myelin做空间PCA
    # vis_name = 'MMP-vis3-R'  # MMP-vis3-L, MMP-vis3-R
    # fname = f'HCPY-M_{vis_name}_zscore1_PCA-subj'
    # decompose(
    #     fpaths=[pjoin(proj_dir, 'data/HCP/HCPY_myelin.dscalar.nii')],
    #     cat_shape=(1, 1), method='PCA', axis=0,
    #     csv_files=[pjoin(work_dir, f'{fname}.csv')],
    #     cii_files=[pjoin(work_dir, f'{fname}.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'{fname}.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois(vis_name))[0]],
    #     map_mask=None, zscore0=None, zscore1='split', n_component=20, random_state=7)

    # 在成人数据上，对左脑或右脑HCP-MMP1_visual-cortex3做zscore
    # 对thickness做空间PCA
    # vis_name = 'MMP-vis3-L'  # MMP-vis3-L, MMP-vis3-R
    # fname = f'HCPY-corrT_{vis_name}_zscore1_PCA-subj'
    # decompose(
    #     fpaths=[pjoin(proj_dir, 'data/HCP/HCPY_corrThickness.dscalar.nii')],
    #     cat_shape=(1, 1), method='PCA', axis=0,
    #     csv_files=[pjoin(work_dir, f'{fname}.csv')],
    #     cii_files=[pjoin(work_dir, f'{fname}.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'{fname}.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois(vis_name))[0]],
    #     map_mask=None, zscore0=None, zscore1='split', n_component=20, random_state=7)

    # 在成人数据上，分别对左右脑HCP-MMP1_visual-cortex3做zscore
    # 联合随机选取的一半被试的myelin和corrThickness做空间PCA
    # half_id_df = pd.read_csv(pjoin(
    #     proj_dir, 'data/HCP/HCPY_SubjInfo_halfID.csv'))
    # half_id = 0
    # half_mask = half_id_df['halfID'].values == half_id
    # vis_mask = 'MMP-vis3-R'  # MMP-vis3-L, MMP-vis3-R
    # fname = f'HCPY-half{half_id}-M+corrT_{vis_mask}_zscore1_PCA-subj'
    # decompose(
    #     fpaths=[
    #         pjoin(proj_dir, 'data/HCP/HCPY_myelin.dscalar.nii'),
    #         pjoin(proj_dir, 'data/HCP/HCPY_corrThickness.dscalar.nii')],
    #     cat_shape=(2, 1), method='PCA', axis=0,
    #     csv_files=[
    #         pjoin(work_dir, f'{fname}_M.csv'),
    #         pjoin(work_dir, f'{fname}_corrT.csv')],
    #     cii_files=[pjoin(work_dir, f'{fname}.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'{fname}.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois(vis_mask))[0]],
    #     map_mask=half_mask, zscore0=None, zscore1='split', n_component=20, random_state=7
    # )

    # pca_rsfc(
    # fpath=pjoin(proj_dir, 'data/HCP/RSFC_MMP-vis3-R2cortex.pkl'),
    # out_comp_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_PCA-comp.dscalar.nii'),
    # out_weight_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_PCA-weight.dscalar.nii'),
    # out_model_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_PCA.pkl'),
    # zscore0=False, n_component=20, random_state=7
    # )
    # pca_rsfc(
    # fpath=pjoin(proj_dir, 'data/HCP/RSFC_MMP-vis3-R2cortex.pkl'),
    # out_comp_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_zscore_PCA-comp.dscalar.nii'),
    # out_weight_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_zscore_PCA-weight.dscalar.nii'),
    # out_model_file=pjoin(work_dir, 'RSFC_MMP-vis3-R2cortex_zscore_PCA.pkl'),
    # zscore0=True, n_component=20, random_state=7
    # )

    # pca_rsfc(
    #     fpath=pjoin(proj_dir, 'data/HCP/HCPY-avg_RSFC-MMP-vis3-R2grayordinate.pkl'),
    #     out_comp_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_PCA-comp.dscalar.nii'),
    #     out_weight_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_PCA-weight.dscalar.nii'),
    #     out_model_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_PCA.pkl'),
    #     zscore0=False, n_component=20, random_state=7
    # )
    # pca_rsfc(
    #     fpath=pjoin(proj_dir, 'data/HCP/HCPY-avg_RSFC-MMP-vis3-R2grayordinate.pkl'),
    #     out_comp_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_zscore_PCA-comp.dscalar.nii'),
    #     out_weight_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_zscore_PCA-weight.dscalar.nii'),
    #     out_model_file=pjoin(work_dir, 'HCPY-avg_RSFC-MMP-vis3-R2grayordinate_zscore_PCA.pkl'),
    #     zscore0=True, n_component=20, random_state=7
    # )

    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='MMP-vis3-R', mask_name1='grayordinate',
    #     dtype='z', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='grayordinate', mask_name1='grayordinate',
    #     dtype='z', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='MMP-vis3-R', mask_name1='grayordinate',
    #     dtype='r', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='grayordinate', mask_name1='grayordinate',
    #     dtype='r', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='MMP-vis3-R', mask_name1='cortex',
    #     dtype='r', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='cortex', mask_name1='cortex',
    #     dtype='r', zscore0=False, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='MMP-vis3-R', mask_name1='grayordinate',
    #     dtype='r', zscore0=True, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='grayordinate', mask_name1='grayordinate',
    #     dtype='r', zscore0=True, n_component=20, random_state=7)
    # pca_HCPY_avg_rsfc_mat(
    #     mask_name0='MMP-vis3-R', mask_name1='MMP-vis3-R',
    #     dtype='z', zscore0=True, n_component=20, random_state=7)
    pca_HCPY_avg_rsfc_mat(
        mask_name0='MMP-vis3-L', mask_name1='MMP-vis3-L',
        dtype='z', zscore0=True, n_component=20, random_state=7)

    # pca_HCP_MT_SW(dataset_name='HCPD', vis_name='MMP-vis3-R', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)
    # pca_HCP_MT_SW(dataset_name='HCPA', vis_name='MMP-vis3-R', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)
    # pca_HCP_MT_SW(dataset_name='HCPD', vis_name='MMP-vis3-L', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)
    # pca_HCP_MT_SW(dataset_name='HCPA', vis_name='MMP-vis3-L', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)
    # pca_HCP_MT_SW(dataset_name='HCPY', vis_name='MMP-vis3-R', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)
    # pca_HCP_MT_SW(dataset_name='HCPY', vis_name='MMP-vis3-L', width=50,
    #               step=10, merge_remainder=True, n_component=10, random_state=7)

    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPD-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10-merge.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPD-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10-merge_param.pkl')
    # )
    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPA-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10-merge.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPA-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10-merge_param.pkl')
    # )
    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPD-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10-merge.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPD-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10-merge_param.pkl')
    # )
    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPA-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10-merge.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPA-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10-merge_param.pkl')
    # )
    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPY-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPY-M+corrT_MMP-vis3-L_zscore1_PCA-subj_SW-width50-step10_param.pkl')
    # )
    # pca_HCP_MT_SW_param(
    #     src_file=pjoin(work_dir, 'HCPY-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10.pkl'),
    #     pc_names=['C1', 'C2'],
    #     out_file=pjoin(work_dir, 'HCPY-M+corrT_MMP-vis3-R_zscore1_PCA-subj_SW-width50-step10_param.pkl')
    # )

    # get_MT_PC_individual()
    # 对HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC1做空间PCA
    # fname = 'HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC1'
    # decompose(
    #     fpaths=[pjoin(work_dir, f'{fname}.dscalar.nii')],
    #     cat_shape=(1, 1), method='PCA', axis=0,
    #     csv_files=[pjoin(work_dir, f'{fname}_PCA-subj.csv')],
    #     cii_files=[pjoin(work_dir, f'{fname}_PCA-subj.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'{fname}_PCA-subj.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois('MMP-vis3-R'))[0]],
    #     map_mask=None, zscore0=None, zscore1=None, n_component=20, random_state=7)

    # 对HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC2做空间PCA
    # fname = 'HCPY-1069_MMP-vis3-R_M+corrT-zscore_PC2'
    # decompose(
    #     fpaths=[pjoin(work_dir, f'{fname}.dscalar.nii')],
    #     cat_shape=(1, 1), method='PCA', axis=0,
    #     csv_files=[pjoin(work_dir, f'{fname}_PCA-subj.csv')],
    #     cii_files=[pjoin(work_dir, f'{fname}_PCA-subj.dscalar.nii')],
    #     pkl_file=pjoin(work_dir, f'{fname}_PCA-subj.pkl'),
    #     vtx_masks=[Atlas('HCP-MMP').get_mask(get_rois('MMP-vis3-R'))[0]],
    #     map_mask=None, zscore0=None, zscore1=None, n_component=20, random_state=7)

    # beh_data_for_PCA()
    # pca_beh_data()
