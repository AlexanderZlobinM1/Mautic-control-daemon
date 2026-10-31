from pathlib import Path
import pytest


def test_agent_source_has_no_dns_mutation_or_dns_secret_delivery_paths():
    package = Path(__file__).parents[1] / 'mcd_agent'
    forbidden = ('--dns-cloudflare', 'dns_cloudflare_api_token', '/dns-credentials/',
                 'api.cloudflare.com', 'change_resource_record_sets', '/dns_records')
    findings=[]
    for source in package.rglob('*.py'):
        text=source.read_text()
        for value in forbidden:
            if value in text:
                findings.append((str(source.relative_to(package)), value))
    assert findings == []


def test_legacy_dns_credential_argument_is_rejected_before_execution():
    from mcd_agent import cli
    parser=cli._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(['mautic-image','install','--image-ref','fixture',
                           '--domain','fixture.example.org','--php-version','8.4',
                           '--certbot-dns-credential-ref','mcc://fixture'])
    assert exc.value.code == 2
