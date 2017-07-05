import nipype.pipeline as pe
from nipype.interfaces.io import SelectFiles
from nipype.interfaces.utility import IdentityInterface


def _check_params(analysis_info):
    """ Checks analysis_info parameters for 3T workflow. """
    if 'subjects' not in analysis_info.keys():
        raise ValueError("Parameter 'subjects' must be included in analysis_info.")

    if 'sessions' not in analysis_info.keys():
        # Sets 'sessions' to None if necessary.
        analysis_info['sessions'] = None

    return analysis_info


def _configure_iterables(analysis_info, wf):
    """ Configures info/datasource based on subject/session iterables. """

    if analysis_info['sessions'] is None:
        # No session iterable
        infosource = pe.Node(IdentityInterface(fields=['sub_id']), name="infosource")
        infosource.iterables = ('sub_id', analysis_info['subjects'])
        datasource_templates = dict(func='{sub_id}/func/*_bold.nii.gz',
                                    anat='{sub_id}/anat/*_T1w.nii.gz')
        datasource = pe.Node(SelectFiles(datasource_templates, sort_filelist=True),
                             name='datasource')
        wf.connect(infosource, 'sub_id', datasource, 'sub_id')

    else:
        # Both subject and session iterable
        infosource = pe.Node(IdentityInterface(fields=['sub_id']), name="infosource")
        infosource.iterables = [('sub_id', analysis_info['subjects']),
                                ('sess_id', analysis_info['sessions'])]
        datasource_templates = dict(func='{sub_id}/{sess_id}/func/*_bold.nii.gz',
                                    anat='{sub_id}/{sess_id}/anat/*_T1w.nii.gz')
        datasource = pe.Node(SelectFiles(datasource_templates, sort_filelist=True),
                             name='datasource')
        wf.connect(infosource, 'sub_id', datasource, 'sub_id')
        wf.connect(infosource, 'sess_id', datasource, 'sess_id')

    return wf, datasource, infosource


def create_all_3T_workflow(analysis_info, name='all_3T'):

    import nipype.pipeline as pe
    from nipype.interfaces import fsl
    from nipype.interfaces.utility import Function, IdentityInterface
    from nipype.interfaces.io import DataSink
    from spynoza.nodes.utils import pickfirst, get_scaninfo, concat_iterables
    from spynoza.workflows.motion_correction import create_motion_correction_workflow
    from spynoza.workflows.registration import create_registration_workflow
    from spynoza.nodes import savgol_filter

    # the actual top-level workflow
    all_3T_workflow = pe.Workflow(name=name)

    rename_container = pe.Node(Function(input_names=['sub', 'sess'],
                                        output_names=['out_file'],
                                        function=concat_iterables),
                               name='concat_it')

    analysis_info = _check_params(analysis_info)
    all_3T_workflow, datasource, infosource = _configure_iterables(analysis_info, all_3T_workflow)

    input_node = pe.Node(IdentityInterface(
        fields=['raw_directory', 'output_directory',
                'which_file_is_EPI_space', 'standard_file',
                'smoothing']), name='inputspec')

    all_3T_workflow.connect(input_node, 'raw_directory', datasource, 'base_directory')
    all_3T_workflow.connect(infosource, 'sub_id', rename_container, 'sub')

    if analysis_info['sessions'] is not None:
        all_3T_workflow.connect(infosource, 'sess_id', rename_container, 'sess')

    datasink = pe.Node(DataSink(), name='sinker')
    datasink.inputs.parameterization = False
    all_3T_workflow.connect(input_node, 'output_directory', datasink, 'base_directory')
    all_3T_workflow.connect(rename_container, 'out_file', datasink, 'container')

    # Basic preprocessing
    reorient_epi = pe.MapNode(interface=fsl.Reorient2Std(), name='reorient_epi', iterfield='in_file')
    bet_epi = pe.MapNode(interface=fsl.BET(frac=0.4, functional=True), name='bet_epi', iterfield='in_file')
    all_3T_workflow.connect(datasource, 'func', reorient_epi, 'in_file')
    all_3T_workflow.connect(reorient_epi, 'out_file', bet_epi, 'in_file')

    reorient_T1 = pe.Node(interface=fsl.Reorient2Std(), name='reorient_T1')
    bet_T1 = pe.Node(interface=fsl.BET(frac=0.4, functional=False), name='bet_T1')
    all_3T_workflow.connect(bet_T1, 'out_file', datasink, 'betted_T1')
    all_3T_workflow.connect(datasource, 'anat', reorient_T1, 'in_file')
    all_3T_workflow.connect(reorient_T1, 'out_file', bet_T1, 'in_file')

    # motion correction
    motion_proc = create_motion_correction_workflow('moco')
    all_3T_workflow.connect(input_node, 'output_directory', motion_proc, 'inputspec.output_directory')
    all_3T_workflow.connect(input_node, 'which_file_is_EPI_space', motion_proc, 'inputspec.which_file_is_EPI_space')
    all_3T_workflow.connect(bet_epi, 'out_file', motion_proc, 'inputspec.in_files')
    all_3T_workflow.connect(rename_container, 'out_file', motion_proc, 'inputspec.sub_id')

    # registration
    reg = create_registration_workflow(analysis_info, name='reg')
    all_3T_workflow.connect(rename_container, 'out_file', reg, 'inputspec.sub_id')
    all_3T_workflow.connect(motion_proc, 'outputspec.EPI_space_file', reg, 'inputspec.EPI_space_file')
    all_3T_workflow.connect(input_node, 'output_directory', reg, 'inputspec.output_directory')
    all_3T_workflow.connect(input_node, 'standard_file', reg, 'inputspec.standard_file')

    # the T1_file entry could be empty sometimes, depending on the output of the
    # datasource. Check this.
    all_3T_workflow.connect(datasource, ('anat', pickfirst), reg, 'inputspec.T1_file')

    extract_scaninfo = pe.MapNode(Function(input_names=['in_file'],
                                             output_names=['TR', 'shape', 'dyns', 'voxsize', 'affine'],
                                             function=get_scaninfo),
                               name='extract_scaninfo', iterfield='in_file')

    all_3T_workflow.connect(datasource, 'func', extract_scaninfo, 'in_file')

    slicetimer = pe.MapNode(interface=fsl.SliceTimer(interleaved=False),
                         name='slicetimer', iterfield=['in_file', 'time_repetition'])

    all_3T_workflow.connect(extract_scaninfo, 'TR', slicetimer, 'time_repetition')
    all_3T_workflow.connect(motion_proc, 'outputspec.motion_corrected_files', slicetimer, 'in_file')

    smooth = pe.MapNode(interface=fsl.IsotropicSmooth(),
                     name='smooth', iterfield='in_file')

    all_3T_workflow.connect(input_node, 'smoothing', smooth, 'fwhm')
    all_3T_workflow.connect(slicetimer, 'slice_time_corrected_file', smooth, 'in_file')

    # node for temporal filtering
    sgfilter = pe.MapNode(Function(input_names=['in_file'],
                                   output_names=['out_file'],
                                   function=savgol_filter),
                          name='sgfilter', iterfield=['in_file'])

    all_3T_workflow.connect(smooth, 'out_file', sgfilter, 'in_file')
    all_3T_workflow.connect(sgfilter, 'out_file', datasink, 'clean_func')

    return all_3T_workflow

if __name__ == '__main__':
    from nipype.interfaces.fsl import Info

    analysis_info = {'use_FS': False,
                    'do_fnirt': False,
                    'subjects': ['sub-0028', 'sub-0029']}

    all_3T = create_all_3T_workflow(analysis_info)
    all_3T.base_dir = '/media/lukas/data/Spynoza_data/data_piop'

    template = Info.standard_image('MNI152_T1_2mm_brain.nii.gz')

    all_3T.inputs.inputspec.output_directory = '/media/lukas/data/Spynoza_data/data_piop/preproc'
    all_3T.inputs.inputspec.raw_directory = '/media/lukas/data/Spynoza_data/data_piop'
    all_3T.inputs.inputspec.standard_file = template
    all_3T.inputs.inputspec.which_file_is_EPI_space = 'middle'
    all_3T.inputs.inputspec.smoothing = 3

    all_3T.config = {'execution': {'stop_on_first_crash': True,
                     'keep_inputs': True,
                     'remove_unnecessary_outputs': True}}

    graph = all_3T.run('MultiProc', plugin_args={'n_procs': 1})
