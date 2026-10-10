"""Build the vendored native tray without requiring a .NET SDK or Visual Studio."""
from __future__ import annotations
import os
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'source/quota_tray'

def build(run_tests=True):
    folder = ROOT / '.build/quota'
    folder.mkdir(parents=True, exist_ok=True)
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    compiler = windows / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    references = ['System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Web.Extensions.dll']
    for assembly in ('UIAutomationClient', 'UIAutomationTypes', 'WindowsBase', 'PresentationCore', 'PresentationFramework'):
        candidates = list((windows / 'Microsoft.NET/assembly').glob(f'GAC_*/{assembly}/*/{assembly}.dll'))
        if not candidates: raise RuntimeError(f'Missing Windows framework assembly: {assembly}')
        references.append(str(candidates[0]))
    base = [str(compiler), '/nologo', '/codepage:65001', '/optimize+', *('/reference:' + r for r in references)]
    def compile(name, paths, gui=False):
        arguments = [*base, '/target:' + ('winexe' if gui else 'exe'), '/out:' + str(folder/name),
                     '/win32manifest:' + str(SOURCE/'app.manifest'), *(str(p) for p in paths)]
        result = subprocess.run(arguments, capture_output=True)
        output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        (folder / (name + '.build.txt')).write_text(output, encoding='utf-8')
        if result.returncode: raise RuntimeError(output)
    sources = sorted((SOURCE/'src').glob('*.cs'))
    compile('CodexUsageTray.exe', sources, True)
    libraries = [p for p in sources if p.name != 'Program.cs']
    compile('QuotaParserTests.exe', [*libraries, SOURCE/'tests/ParserTests.cs'])
    compile('IntegrationTests.exe', [*libraries, SOURCE/'tests/IntegrationTests.cs'])
    compile('CombinedPreview.exe', [*libraries, SOURCE/'tools/CombinedPreview.cs'])
    if run_tests:
        summary = {}
        environment = {**os.environ, 'SESSION_USAGE_STATE_DIR': str(folder/'test-state')}
        for name in ('QuotaParserTests.exe', 'IntegrationTests.exe'):
            result = subprocess.run([str(folder/name)], env=environment, capture_output=True, timeout=30)
            output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
            (folder / (name + '.tests.txt')).write_text(output, encoding='utf-8')
            if result.returncode: raise RuntimeError(output)
            print(output.strip())
            summary[name] = int(re.search(r'(\d+) assertions passed', output).group(1))
        (folder/'test-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return folder / 'CodexUsageTray.exe'

if __name__ == '__main__':
    print(build())
