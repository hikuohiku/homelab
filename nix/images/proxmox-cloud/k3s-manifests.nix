{ ... }:

# K3s Auto-Deploy Manifests (Bootstrap)
# NixOS layer では最低限の ArgoCD + App of Apps のみデプロイ
# 残りのコンポーネント (ESO, Tailscale Operator 等) は ArgoCD App of Apps で管理
# doppler-token Secret は sops.templates で生成
{
  services.k3s.manifests = {
    # ====================
    # 1. Namespace 作成
    # ====================
    namespaces = {
      content = [
        {
          apiVersion = "v1";
          kind = "Namespace";
          metadata.name = "argocd";
        }
        {
          apiVersion = "v1";
          kind = "Namespace";
          metadata.name = "external-secrets";
        }
      ];
    };

    # ====================
    # 2. ArgoCD HelmChart (最低限の設定)
    # ====================
    # Tailscale LoadBalancer 等の設定は apps/argocd/values.yaml で管理
    # ArgoCD が自己管理で values を上書きする
    argocd = {
      content = {
        apiVersion = "helm.cattle.io/v1";
        kind = "HelmChart";
        metadata = {
          name = "argocd";
          namespace = "kube-system";
        };
        spec = {
          repo = "https://argoproj.github.io/argo-helm";
          chart = "argo-cd";
          version = "9.1.6";
          targetNamespace = "argocd";
          valuesContent = builtins.toJSON {
            # Kustomize Helm Inflation を有効化 (App of Apps で必要)
            configs.cm."kustomize.buildOptions" = "--enable-helm";
          };
        };
      };
    };

    # ====================
    # 3. ArgoCD App of Apps
    # ====================
    # このアプリケーションが apps/ 以下の全てをデプロイ:
    # - external-secrets (ESO + ClusterSecretStore + ExternalSecret)
    # - tailscale-operator
    # - argocd (自己管理で values を上書き)
    apps = {
      content = {
        apiVersion = "argoproj.io/v1alpha1";
        kind = "Application";
        metadata = {
          name = "apps";
          namespace = "argocd";
        };
        spec = {
          project = "default";
          source = {
            repoURL = "https://github.com/hikuohiku/homelab.git";
            targetRevision = "HEAD";
            path = "apps";
          };
          destination = {
            server = "https://kubernetes.default.svc";
            namespace = "argocd";
          };
          syncPolicy = {
            automated = {
              prune = true;
              selfHeal = true;
            };
            syncOptions = [ "CreateNamespace=true" ];
          };
        };
      };
    };
  };
}
