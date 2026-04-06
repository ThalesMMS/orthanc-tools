#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'USAGE'
Uso:
  ./orthanc-delete-all-studies.sh [opcoes]

Apaga todos os estudos armazenados no Orthanc usando a API REST.

Opcoes:
  --base-url URL             URL base do Orthanc. Ex.: http://127.0.0.1:8042
  --user USUARIO             Usuario HTTP do Orthanc
  --password SENHA           Senha HTTP do Orthanc
  --config-dir DIR           Diretorio de configuracao. Padrao: /etc/orthanc
  --orthanc-config ARQUIVO   Caminho explicito para o orthanc.json
  --credentials-config ARQ   Caminho explicito para o credentials.json
  --dry-run                  Apenas mostra quantos estudos seriam apagados
  --yes                      Nao pede confirmacao interativa
  -h, --help                 Mostra esta ajuda

Variaveis de ambiente equivalentes:
  ORTHANC_BASE_URL
  ORTHANC_ADMIN_USER
  ORTHANC_ADMIN_PASSWORD
  ORTHANC_CONFIG_DIR
  ORTHANC_MAIN_CONFIG_FILE
  ORTHANC_CREDENTIALS_CONFIG_FILE

Exemplos:
  sudo ./orthanc-delete-all-studies.sh --dry-run
  sudo ./orthanc-delete-all-studies.sh --yes
  ./orthanc-delete-all-studies.sh \
    --base-url http://127.0.0.1:8042 \
    --user admin \
    --password 'sua-senha' \
    --yes
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Comando obrigatorio nao encontrado: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd python3

DRY_RUN=false
ASSUME_YES=false
CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
MAIN_CONFIG_FILE="${ORTHANC_MAIN_CONFIG_FILE:-}"
CREDENTIALS_CONFIG_FILE="${ORTHANC_CREDENTIALS_CONFIG_FILE:-}"
ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-}"
ORTHANC_ADMIN_USER="${ORTHANC_ADMIN_USER:-}"
ORTHANC_ADMIN_PASSWORD="${ORTHANC_ADMIN_PASSWORD:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --base-url" >&2; exit 1; }
      ORTHANC_BASE_URL="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --user" >&2; exit 1; }
      ORTHANC_ADMIN_USER="$2"
      shift 2
      ;;
    --password)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --password" >&2; exit 1; }
      ORTHANC_ADMIN_PASSWORD="$2"
      shift 2
      ;;
    --config-dir)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --config-dir" >&2; exit 1; }
      CONFIG_DIR="$2"
      shift 2
      ;;
    --orthanc-config)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --orthanc-config" >&2; exit 1; }
      MAIN_CONFIG_FILE="$2"
      shift 2
      ;;
    --credentials-config)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --credentials-config" >&2; exit 1; }
      CREDENTIALS_CONFIG_FILE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Opcao invalida: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$MAIN_CONFIG_FILE" ]]; then
  MAIN_CONFIG_FILE="$CONFIG_DIR/orthanc.json"
fi
if [[ -z "$CREDENTIALS_CONFIG_FILE" ]]; then
  CREDENTIALS_CONFIG_FILE="$CONFIG_DIR/credentials.json"
fi

load_from_config() {
  local main_cfg="$1"
  local cred_cfg="$2"
  [[ -f "$main_cfg" && -f "$cred_cfg" ]] || return 1

  readarray -t CFG < <(python3 - "$main_cfg" "$cred_cfg" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    main_cfg = json.load(handle)
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    cred_cfg = json.load(handle)

users = cred_cfg.get("RegisteredUsers", {})
if not isinstance(users, dict) or not users:
    raise SystemExit("RegisteredUsers ausente ou vazio em credentials.json.")

user = next(iter(users))
password = users[user]
if not isinstance(password, str):
    raise SystemExit("Senha do Orthanc invalida em credentials.json.")

port = main_cfg.get("HttpPort", 8042)
print(user)
print(password)
print(port)
PY
  )

  if [[ ${#CFG[@]} -lt 3 ]]; then
    echo "Nao foi possivel interpretar os arquivos de configuracao do Orthanc." >&2
    exit 1
  fi

  if [[ -z "$ORTHANC_ADMIN_USER" ]]; then
    ORTHANC_ADMIN_USER="${CFG[0]}"
  fi
  if [[ -z "$ORTHANC_ADMIN_PASSWORD" ]]; then
    ORTHANC_ADMIN_PASSWORD="${CFG[1]}"
  fi
  if [[ -z "$ORTHANC_BASE_URL" ]]; then
    ORTHANC_BASE_URL="http://127.0.0.1:${CFG[2]}"
  fi
}

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" || -z "$ORTHANC_BASE_URL" ]]; then
  load_from_config "$MAIN_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" || true
fi

ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-http://127.0.0.1:8042}"

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" ]]; then
  echo "Credenciais nao informadas. Use --user/--password ou exporte ORTHANC_ADMIN_USER e ORTHANC_ADMIN_PASSWORD." >&2
  exit 1
fi

api_get() {
  local path="$1"
  local response
  if ! response="$(curl -fsS --max-time 60 -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
    "${ORTHANC_BASE_URL}${path}")"; then
    echo "Falha ao acessar ${ORTHANC_BASE_URL}${path}." >&2
    echo "Verifique se o Orthanc esta em execucao e se a URL/credenciais estao corretas." >&2
    exit 1
  fi

  printf '%s' "$response"
}

api_delete() {
  local path="$1"
  if ! curl -fsS --max-time 60 -X DELETE -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
    "${ORTHANC_BASE_URL}${path}" >/dev/null; then
    echo "Falha ao apagar o recurso ${ORTHANC_BASE_URL}${path}." >&2
    exit 1
  fi
}

parse_study_ids() {
  local json_payload="$1"
  if ! python3 -c '
import json
import sys

path = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(f"Resposta vazia da API {path}.")

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Resposta invalida da API {path}: {exc}. Conteudo recebido: {preview!r}"
    )

if not isinstance(data, list):
    raise SystemExit(f"Resposta inesperada da API {path}.")

for item in data:
    if isinstance(item, str):
        print(item)
  ' "/studies" <<<"$json_payload"
  then
    exit 1
  fi
}

parse_list_count() {
  local json_payload="$1"
  local path="$2"
  if ! python3 -c '
import json
import sys

path = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(f"Resposta vazia da API {path}.")

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Resposta invalida da API {path}: {exc}. Conteudo recebido: {preview!r}"
    )

if not isinstance(data, list):
    raise SystemExit(f"Resposta inesperada da API {path}.")

print(len(data))
  ' "$path" <<<"$json_payload"
  then
    exit 1
  fi
}

validate_system_endpoint() {
  local system_json
  if ! system_json="$(api_get "/system")"; then
    exit 1
  fi

  if ! python3 -c '
import json
import sys

base_url = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(
        f"Resposta vazia da API /system em {base_url}. "
        "Esse endpoint deveria retornar JSON do Orthanc."
    )

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Resposta invalida da API /system em {base_url}: {exc}. "
        f"Conteudo recebido: {preview!r}"
    )

if not isinstance(data, dict):
    raise SystemExit(f"Resposta inesperada da API /system em {base_url}.")

name = data.get("Name")
version = data.get("Version")
if not name or not version:
    raise SystemExit(
        f"Resposta incompleta da API /system em {base_url}. "
        "Campos esperados: Name e Version."
    )

print(f"Orthanc detectado: {name} {version}")
  ' "$ORTHANC_BASE_URL" <<<"$system_json"
  then
    exit 1
  fi
}

validate_system_endpoint
study_json="$(api_get "/studies")"
study_ids_raw="$(parse_study_ids "$study_json")"

STUDY_IDS=()
if [[ -n "$study_ids_raw" ]]; then
  while IFS= read -r study_id; do
    [[ -n "$study_id" ]] && STUDY_IDS+=("$study_id")
  done <<<"$study_ids_raw"
fi

study_count="${#STUDY_IDS[@]}"

if [[ "$study_count" -eq 0 ]]; then
  echo "Nenhum estudo encontrado em ${ORTHANC_BASE_URL}."
  exit 0
fi

echo "Orthanc alvo: ${ORTHANC_BASE_URL}"
echo "Estudos encontrados: ${study_count}"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Modo dry-run ativado. Nenhum estudo foi apagado."
  exit 0
fi

if [[ "$ASSUME_YES" != "true" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Sessao nao interativa. Use --yes para confirmar a exclusao em massa." >&2
    exit 1
  fi

  echo
  echo "ATENCAO: esta operacao vai apagar TODOS os estudos do Orthanc."
  read -r -p "Digite DELETE para continuar: " confirmation
  if [[ "$confirmation" != "DELETE" ]]; then
    echo "Operacao cancelada."
    exit 1
  fi
fi

deleted=0
for study_id in "${STUDY_IDS[@]}"; do
  deleted=$((deleted + 1))
  echo "Apagando estudo ${deleted}/${study_count}: ${study_id}"
  api_delete "/studies/${study_id}"
done

remaining_json="$(api_get "/studies")"
remaining_count="$(parse_list_count "$remaining_json" "/studies")"

if [[ "$remaining_count" != "0" ]]; then
  echo "Exclusao concluida com pendencias. Estudos restantes: ${remaining_count}" >&2
  exit 1
fi

echo "Exclusao concluida. Todos os estudos foram removidos."
