"""Read-only live PR/DynamoDB verification, run only after authorized deployment."""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.deploy_demo import DemoConfig, TABLE, _private_json, aws_clients, load_secrets


class VerificationError(ValueError):
    pass


def validate_evidence(repository, number, metadata, record, comments, *, baseline=None):
    """Validate observed service results; no service access in this function."""
    try:
        repository = repository.lower()
        sha = metadata['head']['sha'].lower()
        key = f'{repository}#{number}#{sha}'
        if (not re.fullmatch('[0-9a-f]{40}', sha) or type(number) is not int or number < 1
                or type(metadata['number']) is not int or metadata['number'] != number
                or metadata['base']['repo']['full_name'].lower() != repository
                or record['review_id']['S'] != key or record['head_sha']['S'] != sha
                or record['repository']['S'].lower() != repository or int(record['pr_number']['N']) != number
                or any(record[field]['S'] != value for field, value in (
                    ('status', 'completed'), ('review_source', 'rules'),
                    ('review_provider', 'rules'), ('ai_status', 'not_run')))):
            raise ValueError
        comment_id = int(record['comment_id']['N'])
        if comment_id < 1:
            raise ValueError
        findings = [item['M'] for item in record['review']['M']['findings']['L']]
        expected = {'PY-PICKLE': 3, 'PY-YAML': 4, 'PY-SHELL': 5}
        if len(findings) != 3 or any(finding['severity']['S'] != 'high'
                                   or finding['confidence']['S'] != 'speculative' for finding in findings):
            raise ValueError
        for rule, line in expected.items():
            if not any(f'[Rule {rule}]' in finding['description']['S']
                       and finding['file']['S'] == 'demo_rule_fixture.py'
                       and int(finding['line']['N']) == line for finding in findings):
                raise ValueError
        sources = record['finding_sources']['L']
        if len(sources) != len(findings) or any(source != {'L': [{'S': 'rules'}]} for source in sources):
            raise ValueError
        review_comments = [comment for comment in comments if isinstance(comment.get('body'), str)
                           and f'### Rule-based security checks · commit {sha[:12]}' in comment['body']]
        if len(review_comments) != 1 or review_comments[0]['id'] != comment_id:
            raise ValueError
        body = review_comments[0]['body']
        if ('AI analysis was not performed' not in body
                or any(f'[Rule {rule}]' not in body or f'demo_rule_fixture.py:{line}' not in body
                       for rule, line in expected.items())
                or body.count('HIGH · speculative · demo_rule_fixture.py:') != 3):
            raise ValueError
        result = {'repository': repository, 'pr_number': number, 'head_sha': sha,
                  'review_id': key, 'comment_id': comment_id, 'review_comment_count': 1,
                  'rule_ids': list(expected), 'review_source': 'rules', 'ai_status': 'not_run'}
        if baseline is not None:
            if any(baseline.get(field) != result[field] for field in
                   ('repository', 'pr_number', 'head_sha', 'comment_id', 'review_comment_count')):
                raise ValueError
            # Manual redelivery must be independently evidenced in GitHub/CloudWatch.
            result['comment_unchanged_since_baseline'] = True
        return result
    except (KeyError, TypeError, AttributeError, ValueError):
        raise VerificationError('live_pr_record_and_comment_do_not_match_expected_completed_demo') from None


def observe(config, number, token, *, github_opener, dynamodb_client, baseline=None):
    from auditor.config import Settings
    from auditor.github import GitHubClient

    if type(number) is not int or number < 1:
        raise VerificationError('invalid_pr_number')
    settings = Settings(repository=config.repository, github_token=token, region=config.region,
                        model_id='python-security-rules-v1', table_name=TABLE, review_provider='rules')
    github = GitHubClient(settings, opener=github_opener)
    # Reuse the existing bounded, fixed-origin, no-retry GitHub transport.
    path = f'/repos/{settings.repository.lower()}'
    metadata = github._request(f'{path}/pulls/{number}')
    sha = metadata.get('head', {}).get('sha', '')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-fA-F]{40}', sha):
        raise VerificationError('invalid_live_commit_identity')
    key = f'{settings.repository.lower()}#{number}#{sha.lower()}'
    response = dynamodb_client.get_item(TableName=TABLE, Key={'review_id': {'S': key}}, ConsistentRead=True)
    comments = []
    for page in range(1, 6):
        batch = github._request(f'{path}/issues/{number}/comments?per_page=100&page={page}')
        if not isinstance(batch, list):
            raise VerificationError('invalid_live_comment_list')
        comments.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise VerificationError('comment_list_exceeds_demo_verification_limit')
    return validate_evidence(config.repository, number, metadata, response.get('Item', {}), comments,
                             baseline=baseline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='yxshxsmv/auditor-demo')
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--pr-number', type=int, required=True)
    parser.add_argument('--secrets-file', type=Path, default=Path('.demo-private/secrets.json'))
    parser.add_argument('--output', type=Path, default=Path('.demo-private/first-verification.json'))
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    try:
        from urllib.request import build_opener
        from auditor.github import _NoRedirect

        config = DemoConfig(args.repository, args.region)
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        clients = aws_clients(config.region)
        result = observe(config, args.pr_number, load_secrets(args.secrets_file)['GITHUB_TOKEN'],
            github_opener=build_opener(_NoRedirect()), dynamodb_client=clients['dynamodb'], baseline=baseline)
        result['verified_at'] = datetime.now(timezone.utc).isoformat()
        result['live_pr_and_record_verified'] = True
        result['webhook_delivery_and_lambda_logs'] = 'verify_separately_in_consoles'
        _private_json(args.output, result)
        print(json.dumps(result, indent=2))
        return 0
    except Exception:
        print('Live verification failed; no success evidence written. Check the PR, logs and record.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
