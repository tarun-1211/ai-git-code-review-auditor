"""Bounded deployment transfers with explicit clients. Never logs request data."""

import base64
import hashlib
import re
import time
from pathlib import Path


class TransferError(ValueError):
    def __init__(self, reason, original=None, verification=None):
        super().__init__(reason)
        self.original = original
        self.verification = verification


def error_code(error):
    response = getattr(error, 'response', None)
    detail = response.get('Error') if isinstance(response, dict) else None
    return detail.get('Code') if isinstance(detail, dict) else None


def transport_error(error):
    return isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
        'ConnectionClosedError', 'EndpointConnectionError', 'ConnectTimeoutError',
        'ReadTimeoutError', 'HTTPClientError', 'ProxyConnectionError', 'SSLError'}


def read_with_retry(action, *, sleep=None):
    sleep = sleep or time.sleep
    for attempt in range(3):
        try:
            return action()
        except Exception as error:
            if not transport_error(error) or attempt == 2:
                raise
            sleep(2 ** attempt)


def validate_function(response, arguments, sha256, arn):
    """Compare secrets in memory; never include a response or mismatched value in errors."""
    config = response.get('Configuration') if isinstance(response, dict) else None
    fields = ('FunctionName', 'Runtime', 'Handler', 'Role', 'Architectures', 'MemorySize', 'Timeout')
    expected_sha = base64.b64encode(bytes.fromhex(sha256)).decode('ascii')
    if (not isinstance(config, dict) or any(config.get(key) != arguments[key] for key in fields)
            or config.get('FunctionArn') != arn or config.get('CodeSha256') != expected_sha
            or config.get('PackageType') != 'Zip' or config.get('State') not in ('Pending', 'Active')
            or config.get('Environment') != arguments['Environment']
            or config.get('LastUpdateStatus') == 'Failed' or config.get('Layers')
            or config.get('VpcConfig', {}).get('SubnetIds')
            or config.get('VpcConfig', {}).get('SecurityGroupIds')
            or config.get('FileSystemConfigs') or config.get('KMSKeyArn')
            or config.get('DeadLetterConfig', {}).get('TargetArn')):
        raise TransferError('existing_function_does_not_match_expected_release_and_configuration')
    return {'FunctionArn': arn}


def existing_function(client, arguments, sha256, arn, *, poll=False, sleep=None):
    """Only explicit ResourceNotFound is absence; unknown/read-denied results stop writes."""
    sleep = sleep or time.sleep
    for observation in range(3 if poll else 1):
        try:
            response = read_with_retry(lambda: client.get_function(FunctionName=arguments['FunctionName']), sleep=sleep)
        except Exception as error:
            if error_code(error) != 'ResourceNotFoundException':
                raise
            if poll and observation < 2:
                sleep(2 ** observation)
            continue
        return validate_function(response, arguments, sha256, arn)
    return None


def create_or_reconcile(client, arguments, sha256, arn, *, before_create=lambda: None,
                        record_error=lambda error: None, sleep=None):
    """At most two creates; every ambiguous result is followed by validated GetFunction.

    Three absence observations plus a fresh read are required before the second
    create. A late first creation yields a name conflict, then another read, never
    an update/delete. SDK write retries must remain disabled.
    """
    sleep = sleep or time.sleep
    for attempt in range(2):
        before_create()
        try:
            result = client.create_function(**arguments)
            if result.get('CodeSha256') is not None and result['CodeSha256'] != base64.b64encode(bytes.fromhex(sha256)).decode('ascii'):
                raise TransferError('created_function_code_hash_mismatch_no_update')
            return result
        except Exception as original:
            if not transport_error(original) and error_code(original) != 'ResourceConflictException':
                raise
            try:
                record_error(original)
            except Exception as persistence_error:
                raise TransferError('transport_error_record_could_not_be_saved_no_retry', original, persistence_error) from None
            try:
                found = existing_function(client, arguments, sha256, arn, poll=True, sleep=sleep)
                if found is not None:
                    return found
                if attempt == 1 or error_code(original) == 'ResourceConflictException':
                    raise TransferError('function_not_found_after_bounded_reconciliation', original)
                sleep(2)
                # Recheck immediately before another create, including a late acceptance.
                found = existing_function(client, arguments, sha256, arn, sleep=sleep)
                if found is not None:
                    return found
            except TransferError as error:
                if error.original is not None:
                    raise
                raise TransferError(str(error), original) from None
            except Exception as verification:
                raise TransferError('function_reconciliation_unavailable_no_further_create', original, verification) from None


def stage_package(client, bucket, region, account, artifact, sha256, state, save, *, approved=False, sleep=None):
    """A private same-region bucket and a conditional, checksum-verified object upload.

    Bucket creation requires separate approval. Existing buckets are reused only
    with this deployment's retained creation intent and verified account ownership.
    Neither bucket creation nor an unknown object upload is blindly repeated.
    """
    sleep = sleep or time.sleep
    if not re.fullmatch(r'auditor-demo-packages-[a-z0-9][a-z0-9-]{0,39}[a-z0-9]', bucket):
        raise TransferError('invalid_dedicated_package_bucket_name')
    payload = Path(artifact).read_bytes()
    if hashlib.sha256(payload).hexdigest() != sha256:
        raise TransferError('package_checksum_changed_no_upload')
    key = 'lambda/' + sha256 + '.zip'
    expected = {'bucket': bucket, 'key': key, 'sha256': sha256}
    if state is None and not approved:
        raise TransferError('explicit_package_bucket_approval_required')
    if state is not None and (not isinstance(state, dict)
            or any(state.get(name) != value for name, value in expected.items())
            or state.get('creation_intended') is not True):
        raise TransferError('package_bucket_does_not_match_retained_journal')

    def bucket_exists():
        try:
            read_with_retry(lambda: client.head_bucket(Bucket=bucket, ExpectedBucketOwner=account), sleep=sleep)
            return True
        except Exception as error:
            if error_code(error) in ('404', 'NoSuchBucket', 'NotFound'):
                return False
            raise

    exists = bucket_exists()
    if state is None:
        if exists:
            raise TransferError('unrecorded_package_bucket_exists_no_changes')
        state = {**expected, 'creation_intended': True, 'bucket_created': False, 'uploaded': False}
        save(dict(state))
        arguments = {'Bucket': bucket, 'ObjectOwnership': 'BucketOwnerEnforced'}
        if region != 'us-east-1':
            arguments['CreateBucketConfiguration'] = {'LocationConstraint': region}
        try:
            client.create_bucket(**arguments)
        except Exception as original:
            if not transport_error(original):
                raise
            try:
                if not bucket_exists():
                    raise TransferError('package_bucket_creation_uncertain_no_recreate', original)
            except TransferError:
                raise
            except Exception as verification:
                raise TransferError('package_bucket_creation_unverifiable_no_recreate', original, verification) from None
        state['bucket_created'] = True
        save(dict(state))
    elif not exists:
        raise TransferError('recorded_package_bucket_missing_or_uncertain_no_recreate')
    location = read_with_retry(lambda: client.get_bucket_location(Bucket=bucket, ExpectedBucketOwner=account), sleep=sleep)
    if (location.get('LocationConstraint') or 'us-east-1') != region:
        raise TransferError('package_bucket_region_mismatch_no_upload')
    state['bucket_created'] = True
    save(dict(state))
    # Repeating these exact controls is idempotent and affects only the owned package bucket.
    controls = {'BlockPublicAcls': True, 'IgnorePublicAcls': True,
                'BlockPublicPolicy': True, 'RestrictPublicBuckets': True}
    for action in (
        lambda: client.put_public_access_block(Bucket=bucket, ExpectedBucketOwner=account,
                                               PublicAccessBlockConfiguration=controls),
        lambda: client.put_bucket_ownership_controls(Bucket=bucket, ExpectedBucketOwner=account,
            OwnershipControls={'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]}),
        lambda: client.put_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=account,
            ServerSideEncryptionConfiguration={'Rules': [
                {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}),
    ):
        read_with_retry(action, sleep=sleep)
    checksum = base64.b64encode(bytes.fromhex(sha256)).decode('ascii')

    def object_info():
        try:
            info = read_with_retry(lambda: client.head_object(Bucket=bucket, Key=key,
                ExpectedBucketOwner=account, ChecksumMode='ENABLED'), sleep=sleep)
        except Exception as error:
            if error_code(error) in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise
        if (info.get('ContentLength') != len(payload) or info.get('ChecksumSHA256') != checksum
                or info.get('Metadata', {}).get('sha256') != sha256):
            raise TransferError('existing_package_object_checksum_mismatch_no_overwrite')
        return info

    info = object_info()
    for attempt in range(3):
        if info is not None:
            break
        try:
            client.put_object(Bucket=bucket, Key=key, Body=payload, ContentLength=len(payload),
                ExpectedBucketOwner=account, ServerSideEncryption='AES256',
                ChecksumSHA256=checksum, Metadata={'sha256': sha256}, IfNoneMatch='*')
        except Exception as original:
            if not transport_error(original) and error_code(original) not in ('PreconditionFailed', 'ConditionalRequestConflict'):
                raise
            try:
                info = object_info()
            except Exception as verification:
                raise TransferError('package_upload_unverifiable_no_retry', original, verification) from None
            if info is not None:
                break
            if attempt == 2:
                raise TransferError('package_upload_failed_after_bounded_attempts', original) from None
            sleep(2 ** attempt)
            continue
        info = object_info()
        if info is None:
            raise TransferError('package_upload_not_visible_no_further_write')
    state['uploaded'] = True
    save(dict(state))
    code = {'S3Bucket': bucket, 'S3Key': key}
    if info.get('VersionId') and info['VersionId'] != 'null':
        code['S3ObjectVersion'] = info['VersionId']
    return code
