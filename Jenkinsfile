// TK-ADA 公司 Jenkins 流水线（http://123.207.4.46:8080 任务 tiktok-ad-automation）。
// 公司 Gitea main 收到推送即自动发布：检查 → 构建推送镜像 → 停写备份并恢复验证 → 迁移 → 发布验收。
// 备份与恢复验证都在新加坡集群内完成（备份盘 tiktok-ad-automation-backup），不回传 Jenkins。
// 实际编排就是本机的 deploy/local.py（经 deploy/ci/jenkins-run.sh 在工具镜像中运行），两边共用一套逻辑；
// Jenkins 只负责触发、提供凭据和保存日志。发布状态、检查记录和备份清单/恢复证明在 Jenkins 私有目录
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
        // 16 核 / 32 GiB 节点检查时仍有 16 GiB 以上可用内存、负载低于 4；增加独立数据库分组。
        // 8 组时节点可用内存降到约 0.4 GiB、检查超过 30 分钟（#11，同时有其他任务），保持 6 组。
        // 浏览器 3 worker：工作台页面改由 vite preview 提供构建产物，不再争用开发服务器编译 CPU
        // （本机实测同批 114 项 1 worker 128 秒 → 3 worker 37 秒）。验收各用单独 PostgreSQL。
        CHECK_BACKEND_SHARDS = '6'
        CHECK_ACCEPTANCE_SHARDS = '4'
        CHECK_BROWSER_WORKERS = '3'
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
                    script {
                        def status = sh(returnStatus: true, script: '''set -eu
                            set -- "$ACTION"
                            if [ "$FULL" = true ] && { [ "$ACTION" = deploy ] || [ "$ACTION" = check ]; }; then
                                set -- "$ACTION" --full
                            fi
                            bash deploy/ci/jenkins-run.sh "$@"
                        ''')
                        // 退出码 3：公司 main 已有更新提交，本批在停服前主动放弃，由最新构建发布。
                        if (status == 3) {
                            currentBuild.result = 'NOT_BUILT'
                            echo '已被更新的提交取代，本次未发布。'
                        } else if (status != 0) {
                            error("jenkins-run.sh exited with ${status}")
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            // 外层 shell 被 Jenkins 强制结束时 EXIT trap 也可能失效；按本构建标签再清理一次。
            script {
                if (fileExists('deploy/ci/cleanup-resources.sh')) {
                    sh 'bash deploy/ci/cleanup-resources.sh "$BUILD_TAG"'
                }
            }
            echo "TK-ADA ${params.ACTION}: ${currentBuild.currentResult}"
        }
        failure {
            echo '发布未完成：用 ACTION=status 查看停在哪个阶段；迁移前失败选 recover，候选版本已启动选 finish，迁移已开始需人工核对（见 deploy/release/README.md）。'
        }
    }
}
