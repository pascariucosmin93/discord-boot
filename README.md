# discord-kube-bot

Bot Discord in Python pentru control Kubernetes (read/write): list pods, logs, restart deployment, scale deployment.

## Ce face

Comenzi slash disponibile:
- `/k8s_health`
- `/k8s_get namespace:<ns>`
- `/k8s_logs namespace:<ns> pod:<pod> lines:<1-500>`
- `/k8s_restart namespace:<ns> deployment:<name>`
- `/k8s_scale namespace:<ns> deployment:<name> replicas:<0-100>`

Controale de securitate:
- namespace allowlist (`KUBE_ALLOWED_NAMESPACES`)
- comenzi mutating doar pentru roluri Discord setate în `DISCORD_ALLOWED_ROLE_IDS`
- audit opțional pe webhook (`DISCORD_AUDIT_WEBHOOK_URL`)

## Structură

- `app/main.py` - bot + comenzi Discord + apeluri K8s
- `k8s/` - manifests Kubernetes
- `k8s/argocd/application.yaml` - exemplu Application ArgoCD
- `.github/workflows/release.yml` - build/push + update tag imagine

## CI/CD și versionare imagini x.x.x

Workflow-ul rulează la push pe tag semantic exact `x.x.x` (ex: `1.0.0`):
1. validează formatul tag-ului
2. build + push imagine în `ghcr.io/<owner>/<repo>:x.x.x`
3. actualizează `k8s/deployment.yaml` cu noul tag
4. face commit în `main` (ArgoCD sincronizează)

## Deploy în cluster

1. Creezi secretul real din template:
```bash
cp k8s/secret.example.yaml /tmp/secret.yaml
# editezi valorile reale
kubectl apply -f /tmp/secret.yaml
```

2. Aplici manifests:
```bash
kubectl apply -k k8s
```

3. (Opțional) ArgoCD Application:
- editezi `repoURL` în `k8s/argocd/application.yaml`
- aplici manifestul în namespace-ul unde rulează ArgoCD (`argocd`)

## Secrete GitHub recomandate

Nu este nevoie de token separat pentru GHCR când folosești `GITHUB_TOKEN` + `packages: write`.

## Date pe care trebuie să le completezi

- `DISCORD_TOKEN` (bot token)
- `DISCORD_ALLOWED_ROLE_IDS` (rolurile care pot face restart/scale)
- `DISCORD_AUDIT_WEBHOOK_URL` (opțional)
- `k8s/argocd/application.yaml` -> `repoURL`
- `k8s/deployment.yaml` imaginea inițială placeholder va fi suprascrisă de release

## Notă de securitate

Orice token trimis în clar trebuie rotit. Nu salva tokeni reali în repo; folosește doar Kubernetes Secret / GitHub Secrets.
