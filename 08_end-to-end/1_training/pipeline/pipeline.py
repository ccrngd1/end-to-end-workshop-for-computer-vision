# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Example workflow pipeline script for CustomerChurn pipeline.
                                               . -RegisterModel
                                              .
    Process-> Train -> Evaluate -> Condition .
                                              .
                                               . -(stop)
Implements a get_pipeline(**kwargs) method.
"""

import os

import boto3
import sagemaker
import sagemaker.session

from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.processing import (
    ProcessingInput,
    ProcessingOutput,
    ScriptProcessor,
)

from sagemaker.debugger import (ProfilerConfig,
                                FrameworkProfile,
                                CollectionConfig,
                                DebuggerHookConfig,
                                DetailedProfilingConfig, 
                                DataloaderProfilingConfig, 
                                PythonProfilingConfig,
                                Rule,
                                PythonProfiler,
                                cProfileTimer,
                                ProfilerRule,
                                rule_configs)

from sagemaker.tensorflow import TensorFlow

from sagemaker.workflow.conditions import (
    ConditionGreaterThanOrEqualTo,
)
from sagemaker.workflow.condition_step import (
    ConditionStep,
    JsonGet,
)
from sagemaker.model_metrics import (
    MetricsSource,
    ModelMetrics,
)
from sagemaker.workflow.parameters import (
    ParameterInteger,
    ParameterString,
)
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.steps import ProcessingStep, TrainingStep, CacheConfig, TuningStep

from sagemaker.workflow.step_collections import RegisterModel

import uuid


BASE_DIR = os.path.dirname(os.path.realpath(__file__))


def get_session(region, default_bucket):
    """Gets the sagemaker session based on the region.
    Args:
        region: the aws region to start the session
        default_bucket: the bucket to use for storing the artifacts
    Returns:
        `sagemaker.session.Session instance
    """

    boto_session = boto3.Session(region_name=region)

    sagemaker_client = boto_session.client("sagemaker")
    runtime_client = boto_session.client("sagemaker-runtime")
    return sagemaker.session.Session(
        boto_session=boto_session,
        sagemaker_client=sagemaker_client,
        sagemaker_runtime_client=runtime_client,
        default_bucket=default_bucket,
    )


def get_pipeline(
    region,
    role=None,
    default_bucket=None,
    model_package_group_name="BirdEnd2EndModelGroup",  # Choose any name
    pipeline_name="BirdEnd2EndPipeline",  # You can find your pipeline name in the Studio UI (project -> Pipelines -> name)
    base_job_prefix="BirdEnd2End",  # Choose any name
):
    """Gets a SageMaker ML Pipeline instance working with on CustomerChurn data.
    Args:
        region: AWS region to create and run the pipeline.
        role: IAM role to create and run steps and pipeline.
        default_bucket: the bucket to use for storing the artifacts
    Returns:
        an instance of a pipeline
    """
    sagemaker_session = get_session(region, default_bucket)
    if role is None:
        role = sagemaker.session.get_execution_role(sagemaker_session)
        
    ## By enabling cache, if you run this pipeline again, without changing the input 
    ## parameters it will skip the training part and reuse the previous trained model
    cache_config = CacheConfig(enable_caching=True, expire_after="30d")

    # Parameters for pipeline execution
    processing_instance_count = ParameterInteger(
        name="ProcessingInstanceCount", default_value=1
    )
    processing_instance_type = ParameterString(
        name="ProcessingInstanceType", default_value="ml.m5.xlarge"
    )
    
    training_instance_count = ParameterInteger(
        name="TrainingInstanceCount", default_value=1
    )
    training_instance_type = ParameterString(
        name="TrainingInstanceType", default_value="ml.c5.4xlarge"
    )
    model_approval_status = ParameterString(
        name="ModelApprovalStatus",
        default_value="PendingManualApproval"  # ModelApprovalStatus can be set to a default of "Approved" if you don't want manual approval.
    )
    input_data = ParameterString(
        name="InputDataUrl",
        default_value="s3://sagemaker-us-east-1-909708043314/bird-groundtruth/unlabeled/images",
    )
    
    input_manifest = ParameterString(
        name="InputManifestUrl",
        default_value="s3://sagemaker-us-east-1-909708043314/bird-groundtruth/pipeline/manifest",
    )

    process_image = ParameterString(
        name="ProcessImage",
        default_value="909708043314.dkr.ecr.us-east-1.amazonaws.com/sagemaker-tf-container:2.0",
    )
    
    preprocess_job_name = f"{base_job_prefix}Preprocess"
    # Processing step for feature engineering
    script_processor = ScriptProcessor(
        image_uri=process_image,
        command=["python3"],
        instance_type=processing_instance_type,
        instance_count=processing_instance_count,
        base_job_name=preprocess_job_name,  # choose any name
        sagemaker_session=sagemaker_session,
        role=role,
    )
    
    output_s3_uri = f's3://{default_bucket}/{base_job_prefix}/outputs'#/{uuid.uuid4()}'

    step_process = ProcessingStep(
        name=preprocess_job_name,  # choose any name
        processor=script_processor,
        code=os.path.join(BASE_DIR, "preprocess.py"),
        job_arguments=["--manifest", "manifest",
                       "--images", "images"],
        inputs=[
            ProcessingInput(source=input_data,
                                destination="/opt/ml/processing/input/images/"),
            ProcessingInput(source=input_manifest,
                            destination="/opt/ml/processing/input/manifest/"),
        ],
        outputs=[
            ProcessingOutput(output_name='train_data', 
                             source="/opt/ml/processing/output/train", 
                             destination = output_s3_uri +'/train'),
            ProcessingOutput(source="/opt/ml/processing/output/valid",
                             output_name='val_data',
                             destination = output_s3_uri +'/valid'),
            ProcessingOutput(output_name='test_data',
                             source="/opt/ml/processing/output/test", 
                             destination = output_s3_uri +'/test'),
            ProcessingOutput(output_name='classes',
                             source="/opt/ml/processing/output/classes", 
                             destination = output_s3_uri +'/classes'),
        ],
        cache_config=cache_config
    )

    # Training step for generating model artifacts
    model_path = f"s3://{default_bucket}/{base_job_prefix}/output/models"
    checkpoint_s3_uri = f"s3://{default_bucket}/{base_job_prefix}/output/checkpoints"
    
    TF_FRAMEWORK_VERSION = '2.1.3'
    
    profiler_config = ProfilerConfig(
        system_monitor_interval_millis = 500,
        framework_profile_params = FrameworkProfile(
            detailed_profiling_config = DetailedProfilingConfig(
                start_step = 5, 
                num_steps = 10
            ),
            dataloader_profiling_config = DataloaderProfilingConfig(
                start_step = 7, 
                num_steps = 10
            ),
            python_profiling_config = PythonProfilingConfig(
                start_step = 9, 
                num_steps = 10,
                python_profiler = PythonProfiler.CPROFILE, 
                cprofile_timer = cProfileTimer.TOTAL_TIME
            )
        )
    )
    
    # Set the debugger hook config to save tensors
    debugger_hook_config = DebuggerHookConfig(
        collection_configs = [
            CollectionConfig(name = 'weights'),
            CollectionConfig(name = 'gradients')
        ]
    )

    # Set the rules to analyze tensors emitted during training
    # These specific set of rules will inspect the overall training performance and progress of the model
    rules=[
        ProfilerRule.sagemaker(rule_configs.ProfilerReport()),
        Rule.sagemaker(rule_configs.loss_not_decreasing()),
    ]
    
    hyperparameters = {
        'batch_size': 8,
        'epochs': 15,
        'dropout': 0.76,
        'lr': 0.000019,
        'data_dir': '/opt/ml/input/data'
    }
    
    metric_definitions = [
        {'Name': 'loss', 'Regex': 'loss: ([0-9\\.]+)'},
        {'Name': 'acc', 'Regex': 'accuracy: ([0-9\\.]+)'},
        {'Name': 'val_loss', 'Regex': 'val_loss: ([0-9\\.]+)'},
        {'Name': 'val_acc', 'Regex': 'val_accuracy: ([0-9\\.]+)'}]
    
    if training_instance_count > 1:
        distributions = {
            'mpi': {
                'enabled': True,
                'processes_per_host': 1
            }
        }
        DISTRIBUTION_MODE = 'ShardedByS3Key'
    else:
        distribution = {'parameter_server': {'enabled': False}}
        DISTRIBUTION_MODE = 'FullyReplicated'
                       
    estimator = TensorFlow(entry_point='train_debugger.py',
                           source_dir=os.path.join(BASE_DIR, 'code'),
                           instance_type=training_instance_type,
                           instance_count=training_instance_count,
                           distribution=distribution,
                           hyperparameters=hyperparameters,
                           metric_definitions=metric_definitions,
                           role=role,
                           framework_version=TF_FRAMEWORK_VERSION,
                           py_version='py3',
                           base_job_name=f"{base_job_prefix}-hvd",
                           profiler_config=profiler_config,
                           debugger_hook_config=debugger_hook_config,
                           rules=rules,
                           input_mode='Pipe',
                           script_mode=True,
                           tags=[
                               {
                                   "Key":"TrainingType",
                                   "Value":"OnDemand"
                               }
                           ])

    train_in = TrainingInput(s3_data=step_process.properties.ProcessingOutputConfig.Outputs["train_data"].S3Output.S3Uri,
                             distribution=DISTRIBUTION_MODE)
    val_in   = TrainingInput(s3_data=step_process.properties.ProcessingOutputConfig.Outputs["val_data"].S3Output.S3Uri,
                             distribution=DISTRIBUTION_MODE)

    inputs = {'train':train_in, 'valid': val_in}

    step_train = TrainingStep(
        name=f"{base_job_prefix}Train",
        estimator=estimator,
        inputs=inputs,
        cache_config=cache_config
    )
    
    evaluation_job_name = f"{base_job_prefix}Evaluation"
    # Processing step for evaluation
    script_eval = ScriptProcessor(
        image_uri=process_image,
        command=["python3"],
        instance_type=processing_instance_type,
        instance_count=processing_instance_count,
        base_job_name=evaluation_job_name,
        sagemaker_session=sagemaker_session,
        role=role,
    )
    
    evaluation_report = PropertyFile(
        name="EvaluationReport",
        output_name="evaluation",
        path="evaluation.json",
    )
    step_eval = ProcessingStep(
        name=evaluation_job_name,
        processor=script_eval,
        code=os.path.join(BASE_DIR, "evaluation.py"),
        job_arguments=["--model-file", "model.tar.gz",
                       "--classes-file", "classes.json"],
        inputs=[ProcessingInput(source=step_process.properties.ProcessingOutputConfig.Outputs["test_data"].S3Output.S3Uri, 
                                destination="/opt/ml/processing/input/test"),
                ProcessingInput(source=step_process.properties.ProcessingOutputConfig.Outputs["classes"].S3Output.S3Uri,
                                destination="/opt/ml/processing/input/classes"),
                ProcessingInput(source=step_train.properties.ModelArtifacts.S3ModelArtifacts,
                                destination="/opt/ml/processing/model"),
                ],
        outputs=[
            ProcessingOutput(output_name="evaluation", source="/opt/ml/processing/output"),
        ],
        property_files=[evaluation_report],
        cache_config=cache_config
    )

    # Register model step that will be conditionally executed
    model_metrics = ModelMetrics(
        model_statistics=MetricsSource(
            s3_uri="{}/evaluation.json".format(
                step_eval.arguments["ProcessingOutputConfig"]["Outputs"][0]["S3Output"][
                    "S3Uri"
                ]
            ),
            content_type="application/json",
        )
    )

    # Register model step that will be conditionally executed
    step_register = RegisterModel(
        name=f"{base_job_prefix}RegisterModel",
        estimator=estimator,
        model_data=step_train.properties.ModelArtifacts.S3ModelArtifacts,
        content_types=["text/csv"],
        response_types=["text/csv"],
        inference_instances=["ml.t2.medium", "ml.m5.large"],
        transform_instances=["ml.m5.large"],
        model_package_group_name=model_package_group_name,
        approval_status=model_approval_status,
        model_metrics=model_metrics,
    )

    # Condition step for evaluating model quality and branching execution
    cond_lte = ConditionGreaterThanOrEqualTo(  # You can change the condition here
        left=JsonGet(
            step=step_eval,
            property_file=evaluation_report,
            json_path="multiclass_classification_metrics.accuracy.value",  # This should follow the structure of your report_dict defined in the evaluate.py file.
        ),
        right=0.8,  # You can change the threshold here
    )
    step_cond = ConditionStep(
        name=f"{base_job_prefix}AccuracyCond",
        conditions=[cond_lte],
        if_steps=[step_register],
        else_steps=[],
    )

    # Pipeline instance
    pipeline = Pipeline(
        name=pipeline_name,
        parameters=[
            processing_instance_type,
            processing_instance_count,
            training_instance_count,
            training_instance_type,
            model_approval_status,
            input_data,
            input_manifest,
            process_image,
        ],
        steps=[step_process, step_train, step_eval, step_cond],
        sagemaker_session=sagemaker_session,
    )
    return pipeline