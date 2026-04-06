#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'USAGE'
Uso:
  sudo ./orthanc-diagnose-rest.sh [opcoes]

Diagnostica o endpoint REST do Orthanc e mostra o que a URL configurada
realmente esta retornando para /system e /studies.

Opcoes:
  --base-url URL             URL base do Orthanc. Ex.: http://127.0.0.1:8042
  --user USUARIO             Usuario HTTP do Orthanc
  --password SENHA           Senha HTTP do Orthanc
  --config-dir DIR           Diretorio de configuracao. Padrao: /etc/orthanc
  --orthanc-config ARQUIVO   Caminho explicito para o orthanc.json
  --credentials-config ARQ   Caminho explicito para o credentials.json
  --timeout SEGUNDOS         Timeout de cada chamada HTTP. Padrao: 15
  -h, --help                 Mostra esta ajuda

Variaveis de ambiente equivalentes:
  ORTHANC_BASE_URL
  ORTHANC_ADMIN_USER
  ORTHANC_ADMIN_PASSWORD
  ORTHANC_CONFIG_DIR
  ORTHANC_MAIN_CONFIG_FILE
  ORTHANC_CREDENTIALS_CONFIG_FILE
  ORTHANC_TIMEOUT

Exemplos:
  sudo ./orthanc-diagnose-rest.sh
  sudo ./orthanc-diagnose-rest.sh --base-url http://127.0.0.1:8043
  sudo ./orthanc-diagnose-rest.sh --user admin --password 'sua-senha'
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

CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
MAIN_CONFIG_FILE="${ORTHANC_MAIN_CONFIG_FILE:-}"
CREDENTIALS_CONFIG_FILE="${ORTHANC_CREDENTIALS_CONFIG_FILE:-}"
ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-}"
ORTHANC_ADMIN_USER="${ORTHANC_ADMIN_USER:-}"
ORTHANC_ADMIN_PASSWORD="${ORTHANC_ADMIN_PASSWORD:-}"
TIMEOUT="${ORTHANC_TIMEOUT:-15}"

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
    --timeout)
      [[ $# -ge 2 ]] || { echo "Faltou valor para --timeout" >&2; exit 1; }
      TIMEOUT="$2"
      shift 2
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
  [[ -r "$main_cfg" && -r "$cred_cfg" ]] || return 1

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
name = main_cfg.get("Name", "")
print(user)
print(password)
print(port)
print(name)
PY
  )

  if [[ ${#CFG[@]} -lt 4 ]]; then
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
  if [[ -z "${ORTHANC_NAME_EXPECTED:-}" ]]; then
    ORTHANC_NAME_EXPECTED="${CFG[3]}"
  fi
}

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" || -z "$ORTHANC_BASE_URL" ]]; then
  load_from_config "$MAIN_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" || true
fi

ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-http://127.0.0.1:8042}"

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" ]]; then
  echo "Credenciais nao informadas. Use --user/--password ou rode com sudo para ler /etc/orthanc." >&2
  exit 1
fi

TARGET_PORT="$(python3 - "$ORTHANC_BASE_URL" <<'PY'
import sys
from urllib.parse import urlparse

parsed = urlparse(sys.argv[1])
if parsed.port is not None:
    print(parsed.port)
elif parsed.scheme == "https":
    print(443)
else:
    print(80)
PY
)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

print_section() {
  local title="$1"
  printf '\n== %s ==\n' "$title"
}

preview_file() {
  local path="$1"
  python3 - "$path" <<'PY'
import pathlib
import sys

data = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
data = data.replace("\r", "")
preview = " ".join(data.split())
if not preview:
    print("(vazio)")
elif len(preview) > 300:
    print(preview[:297] + "...")
else:
    print(preview)
PY
}

json_summary() {
  local path="$1"
  local endpoint="$2"
  python3 - "$path" "$endpoint" <<'PY'
import json
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
endpoint = sys.argv[2]

if not payload:
    print(f"JSON: corpo vazio em {endpoint}")
    raise SystemExit(1)

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    print(f"JSON: invalido em {endpoint}: {exc}")
    raise SystemExit(1)

if isinstance(data, dict):
    keys = ", ".join(sorted(data.keys())[:12])
    print(f"JSON: objeto com chaves: {keys}")
    name = data.get("Name")
    version = data.get("Version")
    if endpoint == "/system":
      if name and version:
        print(f"Orthanc: {name} {version}")
      else:
        print("Orthanc: campos Name/Version ausentes")
        raise SystemExit(1)
elif isinstance(data, list):
    print(f"JSON: lista com {len(data)} item(ns)")
else:
    print(f"JSON: tipo inesperado {type(data).__name__}")
    raise SystemExit(1)
PY
}

probe_endpoint() {
  local endpoint="$1"
  local header_file="$TMP_DIR$(printf '%s' "$endpoint" | tr '/' '_').headers"
  local body_file="$TMP_DIR$(printf '%s' "$endpoint" | tr '/' '_').body"
  local curl_exit=0
  local http_code

  http_code="$(curl -sS -D "$header_file" -o "$body_file" \
    --max-time "$TIMEOUT" \
    -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
    -w '%{http_code}' \
    "${ORTHANC_BASE_URL}${endpoint}")" || curl_exit=$?

  print_section "Endpoint ${endpoint}"

  if [[ "$curl_exit" -ne 0 ]]; then
    echo "curl falhou com codigo ${curl_exit}."
    echo "URL: ${ORTHANC_BASE_URL}${endpoint}"
    return 1
  fi

  local body_size
  body_size="$(wc -c <"$body_file" | tr -d ' ')"

  echo "URL: ${ORTHANC_BASE_URL}${endpoint}"
  echo "HTTP: ${http_code}"
  echo "Body bytes: ${body_size}"

  if [[ -s "$header_file" ]]; then
    echo "Headers:"
    sed 's/\r$//' "$header_file"
  else
    echo "Headers: (nenhum header capturado)"
  fi

  echo "Preview:"
  preview_file "$body_file"

  if ! json_summary "$body_file" "$endpoint"; then
    return 1
  fi

  return 0
}

print_section "Configuracao"
echo "Base URL: ${ORTHANC_BASE_URL}"
echo "Usuario: ${ORTHANC_ADMIN_USER}"
if [[ -n "${ORTHANC_NAME_EXPECTED:-}" ]]; then
  echo "Nome esperado: ${ORTHANC_NAME_EXPECTED}"
fi
echo "Timeout: ${TIMEOUT}s"

print_section "Servico"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet orthanc; then
    echo "orthanc.service: active"
  else
    echo "orthanc.service: inactive"
  fi
else
  echo "systemctl indisponivel"
fi

print_section "Porta"
if command -v ss >/dev/null 2>&1; then
  if ! ss -ltnp "( sport = :${TARGET_PORT} )"; then
    echo "Nenhum listener encontrado na porta ${TARGET_PORT}."
  fi
else
  echo "ss indisponivel"
fi

failure=0

probe_endpoint "/system" || failure=1
probe_endpoint "/studies" || failure=1

print_section "Resultado"
if [[ "$failure" -eq 0 ]]; then
  echo "Diagnostico REST concluido sem erros."
  exit 0
fi

echo "O endpoint REST nao respondeu como um Orthanc valido."
echo "Se a porta/base URL estiverem corretas, verifique proxy reverso, autenticacao e a configuracao HTTP do Orthanc."
exit 1
