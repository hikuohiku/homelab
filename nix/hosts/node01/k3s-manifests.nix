{ ... }:

# K3s Auto-Deploy Manifests
# サーバー起動時に /var/lib/rancher/k3s/server/manifests/ に配置され自動適用される
# K3s の HelmChart CRD を使用して Helm チャートを宣言的にデプロイ
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
        {
          apiVersion = "v1";
          kind = "Namespace";
          metadata.name = "tailscale";
        }
      ];
    };

    # ====================
    # 2. ArgoCD HelmChart
    # ====================
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
            # Enable Kustomize Helm Chart Inflation
            configs.cm."kustomize.buildOptions" = "--enable-helm";
            server = {
              service = {
                type = "LoadBalancer";
                loadBalancerClass = "tailscale";
              };
              extraArgs = [ "--insecure" ];
              resources = {
                limits = {
                  cpu = "100m";
                  memory = "128Mi";
                };
                requests = {
                  cpu = "50m";
                  memory = "64Mi";
                };
              };
            };
            controller.resources = {
              limits = {
                cpu = "500m";
                memory = "512Mi";
              };
              requests = {
                cpu = "250m";
                memory = "256Mi";
              };
            };
            repoServer.resources = {
              limits = {
                cpu = "100m";
                memory = "128Mi";
              };
              requests = {
                cpu = "50m";
                memory = "64Mi";
              };
            };
          };
        };
      };
    };

    # ====================
    # 3. External Secrets Operator HelmChart
    # ====================
    external-secrets = {
      content = {
        apiVersion = "helm.cattle.io/v1";
        kind = "HelmChart";
        metadata = {
          name = "external-secrets";
          namespace = "kube-system";
        };
        spec = {
          repo = "https://charts.external-secrets.io";
          chart = "external-secrets";
          version = "1.1.1";
          targetNamespace = "external-secrets";
          valuesContent = builtins.toJSON {
            installCRDs = true;
          };
        };
      };
    };

    # ====================
    # 4. Doppler ClusterSecretStore
    # ====================
    # Note: doppler-token Secret は手動で作成が必要
    cluster-secret-store = {
      content = {
        apiVersion = "external-secrets.io/v1";
        kind = "ClusterSecretStore";
        metadata.name = "doppler";
        spec.provider.doppler.auth.secretRef.dopplerToken = {
          name = "doppler-token";
          key = "token";
          namespace = "external-secrets";
        };
      };
    };

    # ====================
    # 5. Tailscale Operator HelmChart
    # ====================
    tailscale-operator = {
      content = {
        apiVersion = "helm.cattle.io/v1";
        kind = "HelmChart";
        metadata = {
          name = "tailscale-operator";
          namespace = "kube-system";
        };
        spec = {
          repo = "https://pkgs.tailscale.com/helmcharts";
          chart = "tailscale-operator";
          version = "1.90.9";
          targetNamespace = "tailscale";
          valuesContent = builtins.toJSON {
            # OAuth credentials are managed by ExternalSecret
            # ExternalSecret creates 'operator-oauth' Secret in tailscale namespace
            oauth = { };
            operatorConfig.hostname = "k8s-";
            # Enable API server proxy for Tailnet access to Kubernetes API
            apiServerProxyConfig.mode = "noauth";
          };
        };
      };
    };

    # ====================
    # 6. Tailscale OAuth ExternalSecret
    # ====================
    tailscale-oauth-external-secret = {
      content = {
        apiVersion = "external-secrets.io/v1";
        kind = "ExternalSecret";
        metadata = {
          name = "tailscale-oauth";
          namespace = "tailscale";
        };
        spec = {
          refreshInterval = "1h";
          secretStoreRef = {
            kind = "ClusterSecretStore";
            name = "doppler";
          };
          target = {
            name = "operator-oauth";
            creationPolicy = "Owner";
          };
          data = [
            {
              secretKey = "client_id";
              remoteRef.key = "TAILSCALE_CLIENT_ID";
            }
            {
              secretKey = "client_secret";
              remoteRef.key = "TAILSCALE_CLIENT_SECRET";
            }
          ];
        };
      };
    };

    # ====================
    # 7. ArgoCD App of Apps
    # ====================
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
