#!/usr/bin/env bash
set -euo pipefail

readonly SKILL_NAME="pixel-asset-forge"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly SOURCE="${REPO_ROOT}/plugins/pixel-asset-forge/skills/${SKILL_NAME}"
readonly USER_HOME="${HOME:?HOME must be set}"

usage() {
    printf 'Usage: %s [--uninstall]\n' "${0##*/}"
}

install_skill() {
    local skills_dir="$1"
    local platform="$2"
    local destination="${skills_dir}/${SKILL_NAME}"
    local backup="${destination}.bak"

    mkdir -p -- "${skills_dir}"
    if [[ -e "${destination}" || -L "${destination}" ]]; then
        if [[ -d "${destination}" ]] && diff -qr -- "${SOURCE}" "${destination}" >/dev/null; then
            printf '%s: Skill already up to date at %s\n' "${platform}" "${destination}"
            return 0
        fi
        if [[ -e "${backup}" || -L "${backup}" ]]; then
            rm -rf -- "${backup}"
        fi
        mv -- "${destination}" "${backup}"
        printf '%s: existing Skill backed up to %s\n' "${platform}" "${backup}"
    fi

    cp -R -- "${SOURCE}" "${destination}"
    printf '%s: installed Skill at %s\n' "${platform}" "${destination}"
}

uninstall_skill() {
    local skills_dir="$1"
    local platform="$2"
    local destination="${skills_dir}/${SKILL_NAME}"
    local backup="${destination}.bak"

    if [[ -e "${destination}" || -L "${destination}" ]]; then
        rm -rf -- "${destination}"
        printf '%s: removed Skill from %s\n' "${platform}" "${destination}"
    else
        printf '%s: no installed Skill at %s\n' "${platform}" "${destination}"
    fi

    if [[ -e "${backup}" || -L "${backup}" ]]; then
        mv -- "${backup}" "${destination}"
        printf '%s: restored previous Skill from %s\n' "${platform}" "${backup}"
    fi
}

main() {
    local action="install"
    if (( $# > 1 )); then
        usage >&2
        return 2
    fi
    if (( $# == 1 )); then
        case "$1" in
            --uninstall)
                action="uninstall"
                ;;
            --help|-h)
                usage
                return 0
                ;;
            *)
                usage >&2
                return 2
                ;;
        esac
    fi

    if [[ ! -d "${SOURCE}" ]]; then
        printf 'Skill source not found: %s\n' "${SOURCE}" >&2
        return 1
    fi

    if [[ "${action}" == "install" ]]; then
        install_skill "${USER_HOME}/.claude/skills" "Claude Code"
        if [[ -d "${USER_HOME}/.codex" ]]; then
            install_skill "${USER_HOME}/.codex/skills" "Codex"
        else
            printf 'Codex: skipped because %s does not exist\n' "${USER_HOME}/.codex"
        fi
    else
        uninstall_skill "${USER_HOME}/.claude/skills" "Claude Code"
        if [[ -d "${USER_HOME}/.codex" ]]; then
            uninstall_skill "${USER_HOME}/.codex/skills" "Codex"
        else
            printf 'Codex: skipped because %s does not exist\n' "${USER_HOME}/.codex"
        fi
    fi
}

main "$@"
