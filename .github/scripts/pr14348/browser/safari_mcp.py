"""Native Safari MCP smoke and before/after captures against the two real apps.

Run on macOS after `sudo safaridriver --enable`. No request interception or mocks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path
import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = '588e1c4ffefc7ee74a8474da2d4791ea2c44313b'
HEAD = 'bf573a6117540e6187cf8427e3971e97bd74636f'


def require(condition, name):
    if not condition:
        raise AssertionError(name)


def identity(side, client):
    source, home = Path(side['worktree']), Path(side['home'])
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    require(actual == side['sha'], 'source SHA mismatch')
    subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'], check=True)
    require((home / '.uidiff_sha').read_text().strip() == side['sha'], 'home SHA stamp mismatch')
    health = client.get('/api/health').json()
    require(health.get('status') == 'healthy' and health.get('studio_root_id') == side['studio_root_id'], 'server identity mismatch')
    dist = source / 'studio' / 'frontend' / 'dist'
    local = (dist / 'index.html').read_text(encoding='utf-8')
    served = client.get('/').text
    pattern = r'["\x27](/assets/[^"\x27]+)["\x27]'
    assets = set(re.findall(pattern, local))
    require(assets and assets == set(re.findall(pattern, served)), 'served build assets differ')
    hashes = {}
    for asset in assets:
        response = client.get(asset)
        response.raise_for_status()
        expected = (dist / asset.lstrip('/')).read_bytes()
        require(hashlib.sha256(response.content).digest() == hashlib.sha256(expected).digest(), 'served asset hash mismatch')
        hashes[asset] = hashlib.sha256(expected).hexdigest()
    return {'sha': side['sha'], 'server_identity_verified': True, 'served_assets_sha256': hashes}


def rpc(client, side, method, params=None):
    response = client.post('/mcp/', headers={'Authorization': 'Bearer ' + side['api_key'], 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    require(response.status_code == 200, 'MCP HTTP failure')
    try:
        message = response.json()
    except ValueError:
        frames = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        require(bool(frames), 'MCP SSE frames missing')
        message = frames[-1]
    require('result' in message, 'MCP JSON-RPC error')
    return message['result']


def named_button(driver, name):
    for element in driver.find_elements(By.TAG_NAME, 'button'):
        if element.is_displayed() and element.accessible_name == name:
            return element
    return False


def open_settings(driver, side, tab):
    driver.get(side['url'] + '/chat')
    wait = WebDriverWait(driver, 60)
    wait.until(lambda current: named_button(current, 'Settings')).click()
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-settings-dialog]')))
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f'[data-testid="settings-tab-{tab}"]'))).click()
    return wait


def screenshot(driver, path, secrets):
    text = driver.find_element(By.TAG_NAME, 'body').text
    values = driver.execute_script('return [...document.querySelectorAll("input,textarea")].map(e=>e.value).join("\\n")')
    require(all(not value or (value not in text and value not in values) for value in secrets), 'secret rendered in capture surface')
    dialog = driver.find_element(By.CSS_SELECTOR, '[data-settings-dialog]')
    require(dialog.size['width'] > 800, 'unexpected dialog dimensions')
    require(dialog.screenshot(str(path)), 'native Safari screenshot failed')
    return {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    require(platform.system() == 'Darwin', 'native Safari requires macOS')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    require(config['before']['sha'] == BASE and config['after']['sha'] == HEAD, 'unexpected source refs')
    require(config['before']['home'] != config['after']['home'] and config['before']['url'] != config['after']['url'], 'app instances must be isolated')
    args.out.mkdir(parents=True, exist_ok=True)
    report = {'browser': 'native Safari', 'status': 'ERROR', 'sides': {}}
    secrets = [side.get(key, '') for side in config.values() for key in ('access_token', 'refresh_token', 'api_key')]
    driver = None
    try:
        driver = webdriver.Safari()
        driver.set_window_size(1440, 1100)
        report['browser_version'] = driver.capabilities.get('browserVersion')
        for label, tab in (('before', 'api-keys'), ('after', 'mcp')):
            side = config[label]
            with httpx.Client(base_url=side['url'], timeout=30, follow_redirects=True, trust_env=False) as client:
                proof = identity(side, client)
                headers = {'Authorization': 'Bearer ' + side['access_token']}
                payload = {'enabled': True}
                if label == 'after':
                    payload['disabled_tools'] = []
                response = client.put('/api/settings/mcp-access', headers=headers, json=payload)
                require(response.status_code == 200, 'initial settings reset failed')
                names = {tool['name'] for tool in rpc(client, side, 'tools/list')['tools']}
                require(len(names) == 23 and 'chat' in names, 'unexpected initial MCP catalog')
                driver.get(side['url'] + '/')
                seed = {'unsloth_auth_token': side['access_token'], 'theme': 'light', 'palette': 'standard', 'unsloth_locale': 'en'}
                if side.get('refresh_token'):
                    seed['unsloth_refresh_token'] = side['refresh_token']
                driver.execute_script('for(const [key,value] of Object.entries(arguments[0])) localStorage.setItem(key,value)', seed)
                wait = open_settings(driver, side, tab)
                wait.until(lambda current: any(element.is_displayed() and element.accessible_name == 'Allow agent connections' and element.get_attribute('aria-checked') == 'true' for element in current.find_elements(By.CSS_SELECTOR, '[role="switch"]')))
                if label == 'before':
                    require(not driver.find_elements(By.CSS_SELECTOR, '[data-testid="settings-tab-mcp"]'), 'baseline unexpectedly has MCP tab')
                    require(not driver.find_elements(By.CSS_SELECTOR, '[data-mcp-tool]'), 'baseline unexpectedly has per-tool rows')
                    settings = client.get('/api/settings/mcp-access', headers=headers).json()
                    require('tools' not in settings, 'baseline unexpectedly has tool settings')
                    sections = driver.find_elements(By.CSS_SELECTOR, '[data-settings-label="Agent access (MCP)"]')
                    if sections:
                        driver.execute_script('arguments[0].scrollIntoView({block:"center"})', sections[0])
                else:
                    wait.until(lambda current: len(current.find_elements(By.CSS_SELECTOR, '[data-mcp-tool]')) == 23)
                    selector = '[data-mcp-tool="chat"] [role="switch"]'
                    switch = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    require(switch.get_attribute('aria-checked') == 'true', 'chat did not start enabled')
                    driver.execute_script('arguments[0].scrollIntoView({block:"center"})', switch)
                    switch.click()
                    wait.until(lambda current: current.find_element(By.CSS_SELECTOR, selector).get_attribute('aria-checked') == 'false' and current.find_element(By.CSS_SELECTOR, selector).is_enabled())
                    settings = client.get('/api/settings/mcp-access', headers=headers).json()
                    require({tool['name'] for tool in settings['tools'] if not tool['enabled']} == {'chat'}, 'UI toggle did not persist exactly chat disabled')
                    names = {tool['name'] for tool in rpc(client, side, 'tools/list')['tools']}
                    require(len(names) == 22 and 'chat' not in names, 'disabled tool still advertised')
                    denied = rpc(client, side, 'tools/call', {'name': 'chat', 'arguments': {'prompt': 'This must be refused before execution.'}})
                    require(denied.get('isError') is True and '[tool_disabled]' in json.dumps(denied), 'disabled direct call not refused')
                    driver.refresh()
                    wait = open_settings(driver, side, tab)
                    wait.until(lambda current: current.find_element(By.CSS_SELECTOR, selector).get_attribute('aria-checked') == 'false')
                    driver.execute_script('arguments[0].scrollIntoView({block:"center"})', driver.find_element(By.CSS_SELECTOR, selector))
                shot = screenshot(driver, args.out / f'{label}.png', secrets)
                report['sides'][label] = {'identity': proof, 'advertised_tools': len(names), 'chat_advertised': 'chat' in names, 'tool_rows': len(driver.find_elements(By.CSS_SELECTOR, '[data-mcp-tool]')), 'screenshot': shot}
                identity(side, client)
                print(f'PASS native Safari {label} MCP scene', flush=True)
        report['status'] = 'PASS'
    except Exception as exc:
        report['status'] = 'FAIL'
        report['error_type'] = type(exc).__name__
        # Do not serialize WebDriver exceptions: they can contain submitted credentials.
        print(f'FAIL native Safari probe: {type(exc).__name__}', flush=True)
    finally:
        if driver is not None:
            driver.quit()
        (args.out / 'meta.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
