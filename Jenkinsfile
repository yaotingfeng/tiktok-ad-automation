// 公司 Gitea Multibranch Pipeline；main 自动发布，其余分支只有测试。
pipeline {
  agent { label 'tkada-release' }
  options {
    disableConcurrentBuilds() // 不中断正在备份/迁移的旧发布。
    timestamps()
    skipDefaultCheckout(true)
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }
  environment {
    K8S_CONTEXT = 'kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796'
    K8S_NAMESPACE = 'test'
    INGRESS_HOST = 'tkada.shortclipflix.com'
    ACR_HOST = 'sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com'
  }
  stages {
    stage('Checkout') {
      steps {
        checkout scm
        withCredentials([gitUsernamePassword(credentialsId: 'tkada-gitea-read', gitToolName: 'Default')]) {
        sh '''
          git remote get-url company >/dev/null 2>&1 || git remote add company https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git
          git fetch --no-tags company main
        '''
        }
      }
    }
    stage('Tests') {
      steps { sh 'bash deploy/ci/check.sh' }
    }
    stage('Build and publish') {
      when { branch 'main' }
      steps {
        withCredentials([
          gitUsernamePassword(credentialsId: 'tkada-gitea-read', gitToolName: 'Default'),
          usernamePassword(credentialsId: 'tkada-acr-push', usernameVariable: 'ACR_USER', passwordVariable: 'ACR_PASSWORD')
        ]) {
          sh '''
            set +x
            set -eu
            : "${TKADA_STATE_ROOT:?Set an absolute persistent private state directory on the Agent}"
            export DOCKER_CONFIG=$(mktemp -d "$TKADA_STATE_ROOT/docker.XXXXXX")
            trap 'rm -rf "$DOCKER_CONFIG"' EXIT
            printf '%s' "$ACR_PASSWORD" | docker login "$ACR_HOST" -u "$ACR_USER" --password-stdin
            bash deploy/build-and-push.sh
          '''
        }
      }
    }
    stage('Backup, deploy and verify') {
      when { branch 'main' }
      steps {
        withCredentials([
          gitUsernamePassword(credentialsId: 'tkada-gitea-read', gitToolName: 'Default'),
          file(credentialsId: 'tkada-ack-kubeconfig', variable: 'KUBECONFIG'),
          file(credentialsId: 'tkada-retired-source', variable: 'RETIRED_SOURCE_INVENTORY'),
          file(credentialsId: 'tkada-backup-aws', variable: 'AWS_SHARED_CREDENTIALS_FILE'),
          usernamePassword(credentialsId: 'tkada-acr-push', usernameVariable: 'ACR_USER', passwordVariable: 'ACR_PASSWORD')
        ]) {
          sh '''
            set +x
            set -eu
            umask 077
            : "${TKADA_STATE_ROOT:?Configure persistent state outside the workspace}"
            export DOCKER_CONFIG=$(mktemp -d "$TKADA_STATE_ROOT/docker.XXXXXX")
            trap 'rm -rf "$DOCKER_CONFIG"' EXIT
            printf '%s' "$ACR_PASSWORD" | docker login "$ACR_HOST" -u "$ACR_USER" --password-stdin
            release_sha=$(git rev-parse HEAD)
            python3 deploy/release/release.py deploy --sha "$release_sha" --state-dir "$TKADA_STATE_ROOT/release-$release_sha-$BUILD_NUMBER"
          '''
        }
      }
    }
  }
  post {
    failure { echo '发布失败：私有状态和集群锁保留。检查 release.py status，再按手册 recover/finish；禁止直接重跑或回滚数据库。' }
    // 不归档 Secret、数据库备份或私有状态到 Jenkins 可下载的 artifacts。
  }
}
