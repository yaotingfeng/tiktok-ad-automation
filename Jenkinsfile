// TK-ADA 公司 Jenkins 流水线（http://123.207.4.46:8080 任务 tiktok-ad-automation）。
// 公司 Gitea main 收到推送即自动发布：检查 → 构建推送镜像 → 停写备份并恢复验证 → 迁移 → 发布验收。
// 实际编排就是本机的 deploy/local.py（经 deploy/ci/jenkins-run.sh 在工具镜像中运行），两边共用一套逻辑；
// Jenkins 只负责触发、提供凭据和保存日志。发布状态、检查记录和最近一份备份在 Jenkins 私有目录
// TKADA_PRIVATE_DIR（宿主 /data/jenkins/tk-ada-private），含密钥，不作为构建产物归档。
//
// 中断处理与本机一致：失败后手动运行本任务，ACTION 选 status 查看阶段，再按
// deploy/release/README.md 选择 recover 或 finish。

pipeline {
    agent any

    parameters {
        choice(name: 'ACTION', choices: ['deploy', 'check', 'status', 'recover', 'finish'],
            description: 'deploy=检查并发布（推送自动触发时使用）；check=只检查；status/recover/finish=处理中断的发布')
        booleanParam(name: 'FULL', defaultValue: false,
            description: '仅 deploy/check：全量检查（默认只检查受改动影响的部分）')
    }

    environment {
        GIT_URL = 'https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git'
        GIT_CREDS_ID = 'yaotf-gitea-tiktok-ad-automation'
        REGISTRY_CREDS_ID = 'sg-aliyun-harbor-info'
        KUBECONFIG_CREDS_ID = 'sg-aliyun-k8s-info'
        RETIRED_SOURCE_CREDS_ID = 'tiktok-ad-automation-retired-source'
        TOOLS_IMAGE = 'sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation-base:ci-tools-1'
        TKADA_PRIVATE_DIR = '/var/jenkins_home/tk-ada-private'
        // Jenkins 节点不能访问 Docker Hub、海外源较慢：基础镜像用本项目 ACR 副本，下载走国内镜像站。
        TKADA_IMAGE_MIRROR = 'sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation-base:'
        DEBIAN_MIRROR = 'https://mirrors.cloud.tencent.com'
        PYPI_INDEX_URL = 'https://mirrors.aliyun.com/pypi/simple'
        PYPI_FILES_URL = 'https://mirrors.aliyun.com/pypi'
        NPM_REGISTRY = 'https://registry.npmmirror.com'
        // 节点由全公司任务共用（16 核 / 32 GiB）。首次检查中浏览器用例与后端分组同时运行时页面超过
        // 30 秒未响应，单独运行全部通过，因此并发低于本机默认值。
        CHECK_BACKEND_SHARDS = '3'
        CHECK_BROWSER_WORKERS = '1'
    }

    options {
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        timestamps()
        // 完整检查加排空在途任务（最长 20 分钟）与备份恢复验证，留足时间，避免超时打断发布。
        timeout(time: 4, unit: 'HOURS')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('代码拉取') {
            steps {
                // 完整历史：增量检查要对比线上版本，备份要归档线上旧版本源码。
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: 'refs/heads/main']],
                    extensions: [[$class: 'CloneOption', shallow: false, noTags: true, timeout: 20],
                                 [$class: 'CleanCheckout']],
                    userRemoteConfigs: [[url: env.GIT_URL, credentialsId: env.GIT_CREDS_ID]]
                ])
            }
        }

        stage('执行') {
            steps {
                withCredentials([
                    file(credentialsId: env.KUBECONFIG_CREDS_ID, variable: 'KUBECONFIG_FILE'),
                    file(credentialsId: env.RETIRED_SOURCE_CREDS_ID, variable: 'RETIRED_SOURCE_FILE'),
                    usernamePassword(credentialsId: env.REGISTRY_CREDS_ID, usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS'),
                    usernamePassword(credentialsId: env.GIT_CREDS_ID, usernameVariable: 'GIT_USER', passwordVariable: 'GIT_TOKEN')
                ]) {
                    sh '''set -eu
                        set -- "$ACTION"
                        if [ "$FULL" = true ] && { [ "$ACTION" = deploy ] || [ "$ACTION" = check ]; }; then
                            set -- "$ACTION" --full
                        fi
                        bash deploy/ci/jenkins-run.sh "$@"
                    '''
                }
            }
        }
    }

    post {
        always {
            echo "TK-ADA ${params.ACTION}: ${currentBuild.currentResult}"
        }
        failure {
            echo '发布未完成：用 ACTION=status 查看停在哪个阶段；迁移前失败选 recover，候选版本已启动选 finish，迁移已开始需人工核对（见 deploy/release/README.md）。'
        }
    }
}
