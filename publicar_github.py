"""Publica ou atualiza o repositório ReelForge no GitHub via API HTTPS (sem depender do git.exe)."""
import argparse
import base64
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
IGNORE_DIRS = {'.venv', '__pycache__', 'output', '.git', 'logs'}
IGNORE_FILES = {'.env', 'history.sqlite3', 'ReelForge_apenas_codigo.zip'}
IGNORE_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.wav', '.mp3', '.pyc', '.pyo', '.dll', '.zip'}


def should_include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in IGNORE_DIRS for part in rel.parts):
        return False
    if path.name in IGNORE_FILES:
        return False
    # Mantém os áudios pequenos de teste em assets/demo*, mas ignora vídeos/músicas grandes.
    if path.suffix.lower() in IGNORE_EXTS and not any(p.startswith('demo') for p in rel.parts):
        return False
    if path.stat().st_size > 5 * 1024 * 1024:
        return False
    return True


def collect_files():
    files = []
    for p in sorted(ROOT.rglob('*')):
        if p.is_file() and should_include(p):
            files.append(p)
    return files


def publish(token: str, repo_name: str = 'ReelForge', private: bool = False):
    headers = {
        'Authorization': f'Bearer {token.strip()}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    session = requests.Session()
    session.headers.update(headers)

    user_resp = session.get('https://api.github.com/user', timeout=15)
    if user_resp.status_code != 200:
        raise RuntimeError(f'Token inválido ou sem permissão (HTTP {user_resp.status_code}): {user_resp.text}')
    owner = user_resp.json()['login']

    repo_resp = session.get(f'https://api.github.com/repos/{owner}/{repo_name}', timeout=15)
    if repo_resp.status_code == 404:
        print(f'Criando repositório {owner}/{repo_name} no GitHub...')
        create_resp = session.post('https://api.github.com/user/repos', json={
            'name': repo_name,
            'description': 'ReelForge — Gerador de Reels verticais (RSS + Ollama + VoiceStudio + Faster-Whisper + FFmpeg)',
            'private': private,
            'auto_init': True,
        }, timeout=20)
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(f'Falha ao criar repositório (HTTP {create_resp.status_code}): {create_resp.text}')
        repo_data = create_resp.json()
    elif repo_resp.status_code == 200:
        repo_data = repo_resp.json()
        print(f'Repositório {owner}/{repo_name} já existe; atualizando arquivos...')
    else:
        raise RuntimeError(f'Erro ao consultar repositório (HTTP {repo_resp.status_code}): {repo_resp.text}')

    branch = repo_data.get('default_branch') or 'main'
    ref_resp = session.get(f'https://api.github.com/repos/{owner}/{repo_name}/git/ref/heads/{branch}', timeout=15)
    if ref_resp.status_code != 200:
        # Caso o repositório esteja vazio, cria o README inicial via Contents API para inicializar o branch.
        readme = (ROOT / 'README.md').read_bytes() if (ROOT / 'README.md').is_file() else b'# ReelForge\n'
        init_resp = session.put(
            f'https://api.github.com/repos/{owner}/{repo_name}/contents/README.md',
            json={'message': 'Initial commit', 'content': base64.b64encode(readme).decode('ascii'), 'branch': branch},
            timeout=20,
        )
        if init_resp.status_code not in (200, 201):
            raise RuntimeError(f'Erro ao inicializar branch {branch}: {init_resp.text}')
        ref_resp = session.get(f'https://api.github.com/repos/{owner}/{repo_name}/git/ref/heads/{branch}', timeout=15)

    base_commit_sha = ref_resp.json()['object']['sha']
    commit_resp = session.get(f'https://api.github.com/repos/{owner}/{repo_name}/git/commits/{base_commit_sha}', timeout=15)
    base_tree_sha = commit_resp.json()['tree']['sha']

    files = collect_files()
    print(f'Enviando {len(files)} arquivos para {owner}/{repo_name}...')
    tree_items = []
    for path in files:
        rel_posix = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        blob_resp = session.post(
            f'https://api.github.com/repos/{owner}/{repo_name}/git/blobs',
            json={'content': base64.b64encode(raw).decode('ascii'), 'encoding': 'base64'},
            timeout=30,
        )
        if blob_resp.status_code != 201:
            raise RuntimeError(f'Falha ao enviar {rel_posix}: {blob_resp.text}')
        tree_items.append({
            'path': rel_posix,
            'mode': '100644',
            'type': 'blob',
            'sha': blob_resp.json()['sha'],
        })

    tree_resp = session.post(
        f'https://api.github.com/repos/{owner}/{repo_name}/git/trees',
        json={'base_tree': base_tree_sha, 'tree': tree_items},
        timeout=30,
    )
    if tree_resp.status_code != 201:
        raise RuntimeError(f'Erro ao criar árvore Git: {tree_resp.text}')

    new_commit_resp = session.post(
        f'https://api.github.com/repos/{owner}/{repo_name}/git/commits',
        json={
            'message': 'Atualiza ReelForge (roteiro em 2 passos, filtro de qualidade, limpeza e guia CLAUDE.md)',
            'tree': tree_resp.json()['sha'],
            'parents': [base_commit_sha],
        },
        timeout=20,
    )
    if new_commit_resp.status_code != 201:
        raise RuntimeError(f'Erro ao criar commit: {new_commit_resp.text}')

    update_ref = session.patch(
        f'https://api.github.com/repos/{owner}/{repo_name}/git/refs/heads/{branch}',
        json={'sha': new_commit_resp.json()['sha'], 'force': True},
        timeout=20,
    )
    if update_ref.status_code != 200:
        raise RuntimeError(f'Erro ao atualizar branch {branch}: {update_ref.text}')

    html_url = repo_data['html_url']
    print(f'\nSucesso! Repositório publicado em: {html_url}')
    return html_url


def main():
    parser = argparse.ArgumentParser(description='Publica o ReelForge no GitHub via API HTTPS')
    parser.add_argument('--token', default=os.getenv('GITHUB_TOKEN', ''), help='Personal Access Token do GitHub')
    parser.add_argument('--repo', default='ReelForge', help='Nome do repositório (padrão: ReelForge)')
    parser.add_argument('--private', action='store_true', help='Criar como repositório privado')
    args = parser.parse_args()
    token = args.token.strip()
    if not token:
        token = input('Cole seu GitHub Personal Access Token (com permissão repo/public_repo): ').strip()
    if not token:
        print('Erro: informe um token do GitHub.', file=sys.stderr)
        sys.exit(1)
    publish(token, args.repo, args.private)


if __name__ == '__main__':
    main()
