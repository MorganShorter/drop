# Copyright (c) 2019, Djaodjin Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import argparse, configparser, datetime, json, logging, os, random, re, time

import boto3
import botocore.exceptions
import OpenSSL.crypto
import six


LOGGER = logging.getLogger(__name__)
NB_RETRIES = 2
RETRY_WAIT_DELAY = 15


def _check_certificate(public_cert_content, priv_key_content,
                       domain=None, at_time=None):
    """
    Extract the domain names out of the `public_cert_content`.
    """
    result = {}
    # Read the private key and public certificate
    try:
        priv_key = OpenSSL.crypto.load_privatekey(
            OpenSSL.crypto.FILETYPE_PEM, priv_key_content)
    except OpenSSL.crypto.Error as err:
        result.update({'ssl_certificate_key': {
            'state': 'invalid', 'detail': str(err)}})
        priv_key = None

    try:
        public_cert = OpenSSL.crypto.load_certificate(
            OpenSSL.crypto.FILETYPE_PEM, public_cert_content)
    except OpenSSL.crypto.Error as err:
        result.update({'ssl_certificate': {
            'state': 'invalid', 'detail': str(err)}})
        public_cert = None

    if priv_key and public_cert:
        context = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_METHOD)
        context.use_privatekey(priv_key)
        context.use_certificate(public_cert)
        try:
            context.check_privatekey()
        except OpenSSL.SSL.Error:
            result.update({'ssl_certificate': {'state': 'invalid',
                'detail': "certificate does not match private key."}})

    if result:
        raise RuntimeError(result)

    not_after = public_cert.get_notAfter()
    if not isinstance(not_after, six.string_types):
        not_after = not_after.decode('utf-8')
    not_after = datetime.datetime.strptime(not_after, "%Y%m%d%H%M%SZ")
    common_name = public_cert.get_subject().commonName
    alt_names = []
    for ext_idx in range(0, public_cert.get_extension_count()):
        extension = public_cert.get_extension(ext_idx)
        if extension.get_short_name().decode('utf-8') == 'subjectAltName':
            # data of the X509 extension, encoded as ASN.1
            from pyasn1.codec.der.decoder import decode as asn1_decoder
            from pyasn1_modules.rfc2459 import SubjectAltName
            from pyasn1.codec.native.encoder import encode as nat_encoder
            decoded_alt_names, _ = asn1_decoder(
                extension.get_data(), asn1Spec=SubjectAltName())
            for alt in nat_encoder(decoded_alt_names):
                alt_name = alt['dNSName'].decode('utf-8')
                if alt_name != common_name:
                    alt_names += [alt_name]
    if domain:
        found = False
        for alt_name in [common_name] + alt_names:
            regex = alt_name.replace('.', r'\.').replace('*', r'.*') + '$'
            if re.match(regex, domain) or alt_name == domain:
                found = True
                break
        if not found:
            result.update({'ssl_certificate': {'state': 'invalid',
                'detail': "domain name (%s) does not match common or alt names"\
                " present in certificate (%s, %s)." % (
                    domain, common_name, ','.join(alt_names))}})
    if at_time:
        if not_after <= at_time:
            result.update({'ssl_certificate': {'state': 'invalid',
                'detail': "certificate is only valid until %s." % not_after}})

    if result:
        raise RuntimeError(result)

    result.update({'ssl_certificate': {
        'common_name': common_name,
        'alt_names': alt_names,
        'state': result.get('ssl_certificate', {}).get('state', 'valid'),
        'issuer': public_cert.get_issuer().organizationName,
        'ends_at': not_after.isoformat()}})
    return result


def _get_listener(tag_prefix, region_name=None, elb_client=None):
    if not elb_client:
        elb_client = boto3.client('elbv2', region_name=region_name)
    resp = elb_client.describe_load_balancers(
        Names=['%s-elb' % tag_prefix], # XXX matching `create_load_balancer`
    )
    load_balancer = resp['LoadBalancers'][0]
    load_balancer_arn = load_balancer['LoadBalancerArn']
    load_balancer_dns = load_balancer['DNSName']
    LOGGER.info("%s found application load balancer %s available at %s",
        tag_prefix, load_balancer_arn, load_balancer_dns)
    resp = elb_client.describe_listeners(
        LoadBalancerArn=load_balancer_arn)
    for listener in resp['Listeners']:
        if listener['Protocol'] == 'HTTPS':
            https_listener_arn = listener['ListenerArn']
    LOGGER.info("%s found HTTPS listener %s for %s",
        tag_prefix, https_listener_arn, load_balancer_arn)
    return https_listener_arn


def _get_security_group_ids(group_names, tag_prefix,
                            vpc_id=None, ec2_client=None, region_name=None):
    """
    Returns a list of VPC security Group IDs matching one-to-one
    with the `group_names` passed as argument.
    """
    if not ec2_client:
        ec2_client = boto3.client('ec2', region_name=region_name)
    if not vpc_id:
        vpc_id = _get_vpc_id(tag_prefix, ec2_client=ec2_client)
    resp = ec2_client.describe_security_groups(
        Filters=[{'Name': "vpc-id", 'Values': [vpc_id]}])
    group_ids = [None for _ in group_names]
    for security_group in resp['SecurityGroups']:
        for idx, group_name in enumerate(group_names):
            if security_group['GroupName'] == group_name:
                group_ids[idx] = security_group['GroupId']
                LOGGER.info("%s found %s security group %s",
                    tag_prefix, group_name, group_ids[idx])
    return group_ids


def _get_storage_encryption_key(region_name, tag_prefix, kms_client=None):
    kms_key_arn = None
    if not kms_client:
        kms_client = boto3.client('kms', region_name=region_name)
    resp = kms_client.list_keys()
    for key in resp['Keys']:
        try:
            tags_resp = kms_client.list_resource_tags(KeyId=key['KeyId'])
            for tag in tags_resp['Tags']:
                if tag['TagKey'] == 'Prefix' and tag['TagValue'] == tag_prefix:
                    kms_key_arn = key['KeyArn']
                    LOGGER.info("%s found KMS key %s", tag_prefix, kms_key_arn)
                    break
        except botocore.exceptions.ClientError as err:
            # It is possible we can list and use a key but not list the tags
            # This is the case for the "Default master key that protects
            # my ACM private keys when no other key is defined"
            if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'AccessDeniedException':
                raise
        if kms_key_arn:
            break
    return kms_key_arn


def _get_subnet_by_zones(subnet_cidrs, tag_prefix,
                         vpc_id=None, zone_ids=None,
                         ec2_client=None, region_name=None):
    """
    Returns the subnet_id in which databases should be created.
    """
    if not ec2_client:
        ec2_client = boto3.client('ec2', region_name=region_name)
    if not vpc_id:
        vpc_id = _get_vpc_id(tag_prefix, ec2_client=ec2_client)
    if not zone_ids:
        resp = ec2_client.describe_availability_zones()
        zone_ids = sorted([
            zone['ZoneId'] for zone in resp['AvailabilityZones']])
    subnet_by_zones = {}
    for zone_id in zone_ids:
        resp = ec2_client.describe_subnets(Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'availability-zone-id', 'Values': [zone_id]}])
        for subnet in resp['Subnets']:
            for subnet_cidr in subnet_cidrs:
                if subnet['CidrBlock'] == subnet_cidr:
                    subnet_by_zones[zone_id] = subnet['SubnetId']
                    LOGGER.info("%s found subnet %s in zone %s for cidr %s",
                        tag_prefix, subnet_by_zones[zone_id], zone_id,
                        subnet_cidr)
                    break
            if (zone_id in subnet_by_zones and subnet_by_zones[zone_id]):
                break
    return subnet_by_zones


def _get_vpc_id(tag_prefix, ec2_client=None, region_name=None):
    """
    Returns the vpc_id for the application.
    """
    if not ec2_client:
        ec2_client = boto3.client('ec2', region_name=region_name)
    vpc_id = None
    resp = ec2_client.describe_vpcs(
        Filters=[{'Name': 'tag:Prefix', 'Values': [tag_prefix]}])
    if resp['Vpcs']:
        vpc_id = resp['Vpcs'][0]['VpcId']
        LOGGER.info("%s found VPC %s", tag_prefix, vpc_id)
    return vpc_id


def _split_cidrs(vpc_cidr):
    """
    Returns web and dbs subnets cidrs from a `vpc_cidr`.
    """
    dot_parts, length = vpc_cidr.split('/')
    dot_parts = dot_parts.split('.')
    cidr_prefix = '.'.join(dot_parts[:2])
    web_subnet_cidrs = [
        '%s.0.0/20' % cidr_prefix,
        '%s.16.0/20' % cidr_prefix,
        '%s.32.0/20' % cidr_prefix,
        '%s.48.0/20' % cidr_prefix]
    dbs_subnet_cidrs = [
        '%s.64.0/20' % cidr_prefix,
        '%s.128.0/20' % cidr_prefix]
    return web_subnet_cidrs, dbs_subnet_cidrs


def _split_fullchain(fullchain):
    """
    Returns a tuple (certificate, chain) from a fullchain certificate.
    """
    header = '\n-----END CERTIFICATE-----\n'
    crts = fullchain.split(header)
    if crts:
        if crts[-1] == '':
            crts = crts[0:-1]
        certs = [crt + header for crt in crts]
        cert = certs[0]
        chain = ''.join(certs[1:])
        return cert, chain
    raise RuntimeError('invalid fullchain certificate')


def _store_certificate(fullchain, key, domain=None, tag_prefix=None,
                       region_name=None, acm_client=None):
    """
    This will import or replace an ACM certificate for `domain`.

    aws acm import-certificate \
      --certificate file://cert.pem \
      --private-key file://privkey.pem \
      --private-key file://chain.pem \
      --certificate-arn *arn*
    """
    #pylint:disable=unused-argument
    result = _check_certificate(fullchain, key, domain=domain)
    if not domain:
        domain = result['ssl_certificate']['common_name']
    cert, chain = _split_fullchain(fullchain)
    if not acm_client:
        acm_client = boto3.client('acm', region_name=region_name)
    kwargs = {}
    resp = acm_client.list_certificates()
    for acm_cert in resp['CertificateSummaryList']:
        if acm_cert['DomainName'] == domain:
            LOGGER.info("A certificate for domain %s has already been"\
                " imported as %s - replacing",
                domain, acm_cert['CertificateArn'])
            kwargs['CertificateArn'] = acm_cert['CertificateArn']
            break
    resp = acm_client.import_certificate(
        Certificate=cert.encode('ascii'),
        PrivateKey=key.encode('ascii'),
        CertificateChain=chain.encode('ascii'),
        **kwargs)
    LOGGER.info("%s (re-)imported TLS certificate %s as %s",
                tag_prefix, result['ssl_certificate'], resp['CertificateArn'])
    result.update({'CertificateArn': resp['CertificateArn']})
    return result


def create_elb(tag_prefix, web_subnet_by_zones, gate_sg_id,
               tls_priv_key=None, tls_fullchain_cert=None,
               region_name=None):
    """
    Creates the Application Load Balancer.
    """
    elb_client = boto3.client('elbv2', region_name=region_name)
    resp = elb_client.create_load_balancer(
        Name='%s-elb' % tag_prefix,
        Subnets=list(web_subnet_by_zones.values()),
        SecurityGroups=[
            gate_sg_id,
        ],
        Scheme='internet-facing',
        Type='application',
        Tags=[{'Key': "Prefix", 'Value': tag_prefix}])
    load_balancer = resp['LoadBalancers'][0]
    load_balancer_arn = load_balancer['LoadBalancerArn']
    load_balancer_dns = load_balancer['DNSName']
    LOGGER.info("%s found/created application load balancer %s available at %s",
        tag_prefix, load_balancer_arn, load_balancer_dns)

    resp = elb_client.create_listener(
        LoadBalancerArn=load_balancer_arn,
        Protocol='HTTP',
        Port=80,
        DefaultActions=[{
            "Type": "redirect",
            "RedirectConfig": {
                "Protocol": "HTTPS",
                "Port": "443",
                "Host": "#{host}",
                "Path": "/#{path}",
                "Query": "#{query}",
                "StatusCode": "HTTP_301"
            }
        }])
    LOGGER.info("%s found/created application HTTP listener for %s",
        tag_prefix, load_balancer_arn)

    # We will need a default TLS certificate for creating an HTTPS listener.
    default_cert_location = None
    resp = elb_client.describe_listeners(
        LoadBalancerArn=load_balancer_arn)
    for listener in resp['Listeners']:
        if listener['Protocol'] == 'HTTPS':
            for certificate in listener['Certificates']:
                if 'IsDefault' not in certificate or certificate['IsDefault']:
                    default_cert_location = certificate['CertificateArn']
                    LOGGER.info("%s found default TLS certificate %s",
                        tag_prefix, default_cert_location)
                    break
    if not default_cert_location:
        if tls_priv_key and tls_fullchain_cert:
            resp = _store_certificate(
                tls_fullchain_cert, tls_priv_key,
                tag_prefix=tag_prefix, region_name=region_name)
            default_cert_location = resp['CertificateArn']
        else:
            LOGGER.warning("default_cert_location is not set and there are no"\
                " tls_priv_key and tls_fullchain_cert either.")

    resp = elb_client.create_listener(
        LoadBalancerArn=load_balancer_arn,
        Protocol='HTTPS',
        Port=443,
        Certificates=[{'CertificateArn': default_cert_location}],
        DefaultActions=[{
            'Type': 'fixed-response',
            'FixedResponseConfig': {
                'MessageBody': '%s ELB' % tag_prefix,
                'StatusCode': '200',
                'ContentType': 'text/plain'
            }
        }])
    LOGGER.info("%s found/created application load balancer listeners for %s",
        tag_prefix, load_balancer_arn)


def create_network(region_name, vpc_cidr, dbs_zone_names,
                   tls_priv_key=None, tls_fullchain_cert=None,
                   ssh_key_name=None, ssh_key_content=None,
                   sally_ip=None, tag_prefix=None,
                   dry_run=False):
    """
    This function creates in a specified AWS region the network infrastructure
    required to run a SaaS product. It will:

    - create a VPC
    - create a Gateway
    - create proxy and db security groups
    - create an Application ELB
    - create uploads and logs S3 buckets
    - create IAM roles and instance profiles

    (Optional)
    - adds permission to connect from SSH port to security groups
    - import SSH keys
    """
    LOGGER.info("Provisions network ...")
    web_subnet_cidrs, dbs_subnet_cidrs = _split_cidrs(vpc_cidr)

    if not tag_prefix:
        tag_prefix = [random.choice("abcdef")] + "".join(
            [random.choice("abcdef0123456789") for i in range(4)])

    ec2_client = boto3.client('ec2', region_name=region_name)
    resp = ec2_client.describe_availability_zones()
    zone_ids = sorted([zone['ZoneId'] for zone in resp['AvailabilityZones']])
    LOGGER.info("%s web subnets use zone to cidr mapping: %s",
        tag_prefix,
        {zone_id: web_subnet_cidrs[idx]
         for idx, zone_id in enumerate(zone_ids)})
    # makes sure the db_zone_ids is in the same order as the db_zone_names.
    db_zone_ids = []
    for zone_name in dbs_zone_names:
        for zone in resp['AvailabilityZones']:
            if zone['ZoneName'] == zone_name:
                db_zone_ids += [zone['ZoneId']]
                break
    LOGGER.info("%s dbs subnets use zone to cidr mapping: %s",
        tag_prefix,
        {zone_id: dbs_subnet_cidrs[idx]
         for idx, zone_id in enumerate(db_zone_ids)})

    # Create a VPC
    vpc_id = _get_vpc_id(tag_prefix, ec2_client=ec2_client)
    if not vpc_id:
        resp = ec2_client.create_vpc(
            DryRun=dry_run,
            CidrBlock=vpc_cidr,
            AmazonProvidedIpv6CidrBlock=False,
            InstanceTenancy='default')
        vpc_id = resp['Vpc']['VpcId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[vpc_id],
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s-vpc" % tag_prefix}])
        LOGGER.info("%s created VPC %s", tag_prefix, vpc_id)

    # Create subnets for app, dbs and web services
    dbs_subnet_by_zones = _get_subnet_by_zones(
        dbs_subnet_cidrs, tag_prefix,
        vpc_id=vpc_id, zone_ids=zone_ids, ec2_client=ec2_client)
    for idx, zone_id in enumerate(db_zone_ids):
        dbs_subnet_id = dbs_subnet_by_zones.get(zone_id, None)
        if not dbs_subnet_id:
            resp = ec2_client.create_subnet(
                AvailabilityZoneId=zone_id,
                CidrBlock=dbs_subnet_cidrs[idx],
                VpcId=vpc_id,
                DryRun=dry_run)
            dbs_subnet_by_zones[zone_id] = resp['Subnet']['SubnetId']
            dbs_subnet_id = dbs_subnet_by_zones[zone_id]
            ec2_client.create_tags(
                DryRun=dry_run,
                Resources=[dbs_subnet_id],
                Tags=[
                    {'Key': "Prefix", 'Value': tag_prefix},
                    {'Key': "Name",
                     'Value': "%s databases subnet" % tag_prefix}])
            LOGGER.info("%s created dbs subnet %s", tag_prefix, dbs_subnet_id)
            resp = ec2_client.modify_subnet_attribute(
                SubnetId=dbs_subnet_id,
                MapPublicIpOnLaunch={'Value': False})

    web_subnet_by_zones = _get_subnet_by_zones(
        web_subnet_cidrs, tag_prefix,
        vpc_id=vpc_id, zone_ids=zone_ids, ec2_client=ec2_client)
    for idx, zone_id in enumerate(zone_ids):
        web_subnet_id = web_subnet_by_zones.get(zone_id, None)
        if not web_subnet_id:
            resp = ec2_client.create_subnet(
                AvailabilityZoneId=zone_id,
                CidrBlock=web_subnet_cidrs[idx],
                VpcId=vpc_id,
                DryRun=dry_run)
            web_subnet_by_zones[zone_id] = resp['Subnet']['SubnetId']
            web_subnet_id = web_subnet_by_zones[zone_id]
            ec2_client.create_tags(
                DryRun=dry_run,
                Resources=[web_subnet_id],
                Tags=[
                    {'Key': "Prefix", 'Value': tag_prefix},
                    {'Key': "Name",
                     'Value': "%s web subnet" % tag_prefix}])
            LOGGER.info("%s created web subnet %s in zone %s",
                        tag_prefix, web_subnet_id, zone_id)
            resp = ec2_client.modify_subnet_attribute(
                SubnetId=web_subnet_id,
                MapPublicIpOnLaunch={'Value': False})
    app_subnet_id = web_subnet_by_zones[zone_ids[0]]

    # Ensure that the VPC has an Internet Gateway.
    resp = ec2_client.describe_internet_gateways(
        Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])
    if resp['InternetGateways']:
        igw_id = resp['InternetGateways'][0]['InternetGatewayId']
        LOGGER.info("%s found Internet Gateway %s", tag_prefix, igw_id)
    else:
        resp = ec2_client.describe_internet_gateways(
            Filters=[{'Name': 'tag:Prefix', 'Values': [tag_prefix]}])
        if resp['InternetGateways']:
            igw_id = resp['InternetGateways'][0]['InternetGatewayId']
            LOGGER.info("%s found Internet Gateway %s", tag_prefix, igw_id)
        else:
            resp = ec2_client.create_internet_gateway(DryRun=dry_run)
            igw_id = resp['InternetGateway']['InternetGatewayId']
            ec2_client.create_tags(
                DryRun=dry_run,
                Resources=[igw_id],
                Tags=[{'Key': "Prefix", 'Value': tag_prefix},
                      {'Key': "Name",
                       'Value': "%s internet gateway" % tag_prefix}])
            LOGGER.info("%s created Internet Gateway %s", tag_prefix, igw_id)
        resp = ec2_client.attach_internet_gateway(
            DryRun=dry_run,
            InternetGatewayId=igw_id,
            VpcId=vpc_id)

    # Create the NAT gateway by which private subnet connects to Internet
    # XXX Why do we have a Network interface eni-****?
    nat_elastic_ip = None
    sally_elastic_ip = None
    resp = ec2_client.describe_addresses(
        Filters=[{'Name': 'tag:Prefix', 'Values': [tag_prefix]}])
    if resp['Addresses']:
        for resp_address in resp['Addresses']:
            for resp_tag in resp_address['Tags']:
                if resp_tag['Key'] == 'Name':
                    if 'NAT gateway' in resp_tag['Value']:
                        nat_elastic_ip = resp_address['AllocationId']
                        break
                    elif 'Sally' in resp_tag['Value']:
                        sally_elastic_ip = resp_address['AllocationId']
                        break
    if nat_elastic_ip:
        LOGGER.info("%s found NAT gateway public IP %s",
            tag_prefix, nat_elastic_ip)
    else:
        resp = ec2_client.allocate_address(
            DryRun=dry_run,
            Domain='vpc')
        nat_elastic_ip = resp['AllocationId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[nat_elastic_ip],
            Tags=[{'Key': "Prefix", 'Value': tag_prefix},
                  {'Key': "Name",
                   'Value': "%s NAT gateway public IP" % tag_prefix}])
        LOGGER.info("%s created NAT gateway public IP %s",
            tag_prefix, nat_elastic_ip)
    if sally_elastic_ip:
        LOGGER.info("%s found Sally public IP %s",
            tag_prefix, sally_elastic_ip)
    else:
        resp = ec2_client.allocate_address(
            DryRun=dry_run,
            Domain='vpc')
        sally_elastic_ip = resp['AllocationId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[sally_elastic_ip],
            Tags=[{'Key': "Prefix", 'Value': tag_prefix},
                  {'Key': "Name",
                   'Value': "%s Sally public IP" % tag_prefix}])
        LOGGER.info("%s created Sally public IP %s",
            tag_prefix, sally_elastic_ip)

    client_token = tag_prefix
    resp = ec2_client.describe_nat_gateways(Filters=[
        {'Name': "subnet-id", 'Values': [app_subnet_id]},
        {'Name': "state", 'Values': ['pending', 'available']}])
    if resp['NatGateways']:
        nat_gateway_id = resp['NatGateways'][0]['NatGatewayId']
        LOGGER.info("%s found NAT gateway %s", tag_prefix, nat_gateway_id)
    else:
        resp = ec2_client.create_nat_gateway(
            AllocationId=nat_elastic_ip,
            ClientToken=client_token,
            SubnetId=app_subnet_id)
        nat_gateway_id = resp['NatGateway']['NatGatewayId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[nat_gateway_id],
            Tags=[{'Key': "Prefix", 'Value': tag_prefix},
                  {'Key': "Name",
                   'Value': "%s NAT gateway" % tag_prefix}])
        LOGGER.info("%s created NAT gateway %s",
            tag_prefix, nat_gateway_id)

    # Set up public and NAT-protected route tables
    resp = ec2_client.describe_route_tables(
        Filters=[{'Name': "vpc-id", 'Values': [vpc_id]}])
    public_route_table_id = None
    private_route_table_id = None
    for route_table in resp['RouteTables']:
        for route in route_table['Routes']:
            if 'GatewayId' in route and route['GatewayId'] == igw_id:
                public_route_table_id = route_table['RouteTableId']
                LOGGER.info("%s found public route table %s",
                    tag_prefix, public_route_table_id)
                break
            elif ('NatGatewayId' in route and
                  route['NatGatewayId'] == nat_gateway_id):
                private_route_table_id = route_table['RouteTableId']
                LOGGER.info("%s found private route table %s",
                    tag_prefix, private_route_table_id)
    if not public_route_table_id:
        resp = ec2_client.create_route_table(
            DryRun=dry_run,
            VpcId=vpc_id)
        public_route_table_id = resp['RouteTable']['RouteTableId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[public_route_table_id],
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s public" % tag_prefix}])
        LOGGER.info("%s created public subnet route table %s",
            tag_prefix, public_route_table_id)
        resp = ec2_client.create_route(
            DryRun=dry_run,
            DestinationCidrBlock='0.0.0.0/0',
            GatewayId=igw_id,
            RouteTableId=public_route_table_id)
        resp = ec2_client.associate_route_table(
            DryRun=dry_run,
            RouteTableId=public_route_table_id,
            SubnetId=app_subnet_id)
        LOGGER.info(
            "%s associated public route table %s to first web subnet %s",
            tag_prefix, public_route_table_id, app_subnet_id)

    if not private_route_table_id:
        resp = ec2_client.create_route_table(
            DryRun=dry_run,
            VpcId=vpc_id)
        private_route_table_id = resp['RouteTable']['RouteTableId']
        ec2_client.create_tags(
            DryRun=dry_run,
            Resources=[private_route_table_id],
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s internal" % tag_prefix}])
        private_route_table_id = resp['RouteTable']['RouteTableId']
        LOGGER.info("%s created private route table %s",
            tag_prefix, private_route_table_id)
        for _ in range(0, NB_RETRIES):
            # The NAT Gateway takes some time to be fully operational.
            try:
                resp = ec2_client.create_route(
                    DryRun=dry_run,
                    DestinationCidrBlock='0.0.0.0/0',
                    NatGatewayId=nat_gateway_id,
                    RouteTableId=private_route_table_id)
            except botocore.exceptions.ClientError as err:
                if not err.response.get('Error', {}).get(
                        'Code', 'Unknown') == 'InvalidNatGatewayID.NotFound':
                    raise
            time.sleep(RETRY_WAIT_DELAY)
    for idx, zone_id in enumerate(db_zone_ids):
        dbs_subnet_id = dbs_subnet_by_zones[zone_id]
        resp = ec2_client.associate_route_table(
            DryRun=dry_run,
            RouteTableId=private_route_table_id,
            SubnetId=dbs_subnet_id)
        LOGGER.info(
            "%s associated private route table %s to dbs subnet %s",
            tag_prefix, private_route_table_id, dbs_subnet_id)

    # Create the ELB, proxies and databases security groups
    # The app security group (as the instance role) will be specific
    # to the application.
    moat_name = '%s-moat' % tag_prefix
    vault_name = '%s-vault' % tag_prefix
    gate_name = '%s-castle-gate' % tag_prefix
    kitchen_door_name = '%s-kitchen-door' % tag_prefix
    group_ids = _get_security_group_ids(
        [moat_name, vault_name, gate_name, kitchen_door_name],
         tag_prefix, vpc_id=vpc_id, ec2_client=ec2_client)
    moat_sg_id = group_ids[0]
    vault_sg_id = group_ids[1]
    gate_sg_id = group_ids[2]
    kitchen_door_sg_id = group_ids[3]
    if not moat_sg_id:
        resp = ec2_client.create_security_group(
            Description='%s ELB' % tag_prefix,
            GroupName=moat_name,
            VpcId=vpc_id,
            DryRun=dry_run)
        moat_sg_id = resp['GroupId']
        LOGGER.info("%s created %s security group %s",
            tag_prefix, moat_name, moat_sg_id)
    if not gate_sg_id:
        resp = ec2_client.create_security_group(
            Description='%s session managers' % tag_prefix,
            GroupName=gate_name,
            VpcId=vpc_id,
            DryRun=dry_run)
        gate_sg_id = resp['GroupId']
        LOGGER.info("%s created %s security group %s",
            tag_prefix, gate_name, gate_sg_id)
    if not vault_sg_id:
        resp = ec2_client.create_security_group(
            Description='%s databases' % tag_prefix,
            GroupName=vault_name,
            VpcId=vpc_id,
            DryRun=dry_run)
        vault_sg_id = resp['GroupId']
        LOGGER.info("%s created %s security group %s",
            tag_prefix, vault_name, vault_sg_id)
    # moat allow rules
    try:
        resp = ec2_client.authorize_security_group_ingress(
            DryRun=dry_run,
            GroupId=moat_sg_id,
            CidrIp='0.0.0.0/0',
            IpProtocol='tcp',
            FromPort=80,
            ToPort=80)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    try:
        resp = ec2_client.authorize_security_group_ingress(
            DryRun=dry_run,
            GroupId=moat_sg_id,
            CidrIp='0.0.0.0/0',
            IpProtocol='tcp',
            FromPort=443,
            ToPort=443)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    # castle-gate allow rules
    try:
        resp = ec2_client.authorize_security_group_ingress(
            DryRun=dry_run,
            GroupId=gate_sg_id,
            IpPermissions=[{
                'IpProtocol': 'tcp',
                'FromPort': 80,
                'ToPort': 80,
                'UserIdGroupPairs': [{'GroupId': moat_sg_id}]
            }])
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    try:
        resp = ec2_client.authorize_security_group_ingress(
            DryRun=dry_run,
            GroupId=gate_sg_id,
            IpPermissions=[{
                'IpProtocol': 'tcp',
                'FromPort': 443,
                'ToPort': 443,
                'UserIdGroupPairs': [{'GroupId': moat_sg_id}]
            }])
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    try:
        resp = ec2_client.authorize_security_group_egress(
            DryRun=dry_run,
            GroupId=gate_sg_id,
            IpPermissions=[{
                'IpProtocol': '-1',
                'IpRanges': [{
                    'CidrIp': '0.0.0.0/0',
                }]}])
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    # vault allow rules
    try:
        resp = ec2_client.authorize_security_group_ingress(
            DryRun=dry_run,
            GroupId=vault_sg_id,
            IpPermissions=[{
                'IpProtocol': 'tcp',
                'FromPort': 5432,
                'ToPort': 5432,
                'UserIdGroupPairs': [{'GroupId': gate_sg_id}]
            }])
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise
    try:
        resp = ec2_client.authorize_security_group_egress(
            DryRun=dry_run,
            GroupId=vault_sg_id,
            IpPermissions=[{
                'IpProtocol': '-1',
                'IpRanges': [{
                    'CidrIp': '0.0.0.0/0',
                }]}])
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'InvalidPermission.Duplicate':
            raise

    # Create an Application ELB
    create_elb(
        tag_prefix, web_subnet_by_zones, gate_sg_id,
        tls_priv_key=tls_priv_key, tls_fullchain_cert=tls_fullchain_cert,
        region_name=region_name)

    # Create uploads and logs S3 buckets
    s3_logs_bucket = '%s-logs' % tag_prefix
    s3_uploads_bucket = '%s-uploads' % tag_prefix
    s3_client = boto3.client('s3')
    try:
        resp = s3_client.create_bucket(
            ACL='private',
            Bucket=s3_logs_bucket,
            CreateBucketConfiguration={
                'LocationConstraint': region_name
            })
        LOGGER.info("%s found/created S3 bucket for logs %s",
            tag_prefix, s3_logs_bucket)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'BucketAlreadyOwnedByYou':
            raise
    try:
        resp = s3_client.create_bucket(
            ACL='private',
            Bucket=s3_uploads_bucket,
            CreateBucketConfiguration={
                'LocationConstraint': region_name
            })
        LOGGER.info("%s found/created S3 bucket for uploads %s",
            tag_prefix, s3_uploads_bucket)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'BucketAlreadyOwnedByYou':
            raise

    # Creates encryption keys (KMS) in region
    kms_client = boto3.client('kms', region_name=region_name)
    kms_key_arn = _get_storage_encryption_key(region_name, tag_prefix,
        kms_client=kms_client)
    if not kms_key_arn:
        resp = kms_client.create_key(
            Description='%s storage encrypt/decrypt' % tag_prefix,
            Tags=[{'TagKey': "Prefix", 'TagValue': tag_prefix}])
        kms_key_arn = resp['KeyMetadata']['KeyArn']
        LOGGER.info("%s created KMS key %s", tag_prefix, kms_key_arn)

    # Create instance profiles
    gate_role = gate_name
    vault_role = vault_name
    iam_client = boto3.client('iam')
    try:
        resp = iam_client.create_role(
            RoleName=gate_role,
            AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "",
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "ec2.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }))
        iam_client.put_role_policy(
            RoleName=gate_role,
            PolicyName='SendsControlMessagesToAgent',
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": [
                        "sqs:ReceiveMessage",
                        "sqs:DeleteMessage"
                    ],
                    "Effect": "Allow",
                    "Resource": "*"
                }]}))
        iam_client.put_role_policy(
            RoleName=gate_role,
            PolicyName='WriteslogsToStorage',
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": [
                        "s3:PutObject"
                    ],
                    "Effect": "Allow",
                    "Resource": [
                        "arn:aws:s3:::%s/*" % s3_logs_bucket,
                        "arn:aws:s3:::%s" % s3_logs_bucket
                    ]
                }]}))
        iam_client.put_role_policy(
            RoleName=gate_role,
            PolicyName='AccessesUploadedDocuments',
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Action": [
                        "s3:GetObject",
                        # XXX Without `s3:GetObjectAcl` and `s3:ListBucket`
                        # cloud-init cannot run
                        # `aws s3 cp s3://... / --recursive`
                        "s3:GetObjectAcl",
                        "s3:ListBucket",
                        "s3:PutObject"
                    ],
                    "Effect": "Allow",
                    "Resource": [
                        "arn:aws:s3:::%s" % s3_uploads_bucket,
                        "arn:aws:s3:::%s/*" % s3_uploads_bucket
                    ]
                }]}))
        LOGGER.info("%s created IAM role %s", tag_prefix, gate_role)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'EntityAlreadyExists':
            raise
        LOGGER.info("%s found IAM role %s", tag_prefix, gate_role)
    try:
        resp = iam_client.create_instance_profile(
            InstanceProfileName=gate_role)
        iam_instance_profile = resp['InstanceProfile']['Arn']
        LOGGER.info("%s created IAM instance profile '%s'",
            tag_prefix, iam_instance_profile)
        iam_client.add_role_to_instance_profile(
            InstanceProfileName=gate_role,
            RoleName=gate_role)
        LOGGER.info("%s created IAM instance profile for %s: %s",
            tag_prefix, gate_role, iam_instance_profile)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'EntityAlreadyExists':
            raise
        LOGGER.info("%s found IAM instance profile for %s",
            tag_prefix, gate_role)

    # Create role and instance profile for databases
    try:
        resp = iam_client.create_role(
            RoleName=vault_name,
            AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "",
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "ec2.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }))
        iam_client.put_role_policy(
            RoleName=vault_role,
            PolicyName='WriteslogsToStorage',
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    # XXX We are uploading logs
                    "Action": [
                        "s3:PutObject"
                    ],
                    "Effect": "Allow",
                    "Resource": [
                        "arn:aws:s3:::%s/*" % s3_logs_bucket,
                        "arn:aws:s3:::%s" % s3_logs_bucket
                    ]
                }]
            }))
        LOGGER.info("%s created IAM role %s", tag_prefix, vault_name)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'EntityAlreadyExists':
            raise
        LOGGER.info("%s found IAM role %s", tag_prefix, vault_name)

    try:
        resp = iam_client.create_instance_profile(
            InstanceProfileName=vault_role)
        iam_instance_profile = resp['InstanceProfile']['Arn']
        LOGGER.info("%s created IAM instance profile '%s'",
            tag_prefix, iam_instance_profile)
        iam_client.add_role_to_instance_profile(
            InstanceProfileName=vault_role,
            RoleName=vault_role)
        LOGGER.info("%s created IAM instance profile for %s: %s",
            tag_prefix, vault_role, iam_instance_profile)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'EntityAlreadyExists':
            raise
        LOGGER.info("%s found IAM instance profile for %s",
            tag_prefix, vault_role)

    if ssh_key_name and ssh_key_content and sally_ip:
        # import SSH keys
        try:
            resp = ec2_client.import_key_pair(
                DryRun=dry_run,
                KeyName=ssh_key_name,
                PublicKeyMaterial=ssh_key_content)
            LOGGER.info("%s imported SSH key %s", tag_prefix, ssh_key_name)
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'InvalidKeyPair.Duplicate':
                raise
            LOGGER.info("%s found SSH key %s", tag_prefix, ssh_key_name)

        # Create role and instance profile for sally (aka kitchen door)
        kitchen_door_role = kitchen_door_name
        try:
            resp = iam_client.create_role(
                RoleName=kitchen_door_role,
                AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "",
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "ec2.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole"
                    }
                ]
            }))
            iam_client.put_role_policy(
                RoleName=kitchen_door_role,
                PolicyName='WriteslogsToStorage',
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        # XXX We are uploading logs
                        "Action": [
                            "s3:PutObject"
                        ],
                        "Effect": "Allow",
                        "Resource": [
                            "arn:aws:s3:::%s/*" % s3_logs_bucket,
                            "arn:aws:s3:::%s" % s3_logs_bucket
                        ]
                    }]
                }))
            LOGGER.info("%s created IAM role %s", tag_prefix, kitchen_door_role)
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'EntityAlreadyExists':
                raise
            LOGGER.info("%s found IAM role %s", tag_prefix, kitchen_door_role)

        try:
            resp = iam_client.create_instance_profile(
                InstanceProfileName=kitchen_door_role)
            iam_instance_profile = resp['InstanceProfile']['Arn']
            LOGGER.info("%s created IAM instance profile '%s'",
                tag_prefix, iam_instance_profile)
            iam_client.add_role_to_instance_profile(
                InstanceProfileName=kitchen_door_role,
                RoleName=kitchen_door_role)
            LOGGER.info("%s created IAM instance profile for %s: %s",
                tag_prefix, kitchen_door_role, iam_instance_profile)
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'EntityAlreadyExists':
                raise
            LOGGER.info("%s found IAM instance profile for %s",
                tag_prefix, kitchen_door_role)

        # allows SSH connection to instances for debugging
        if not kitchen_door_sg_id:
            resp = ec2_client.create_security_group(
                Description='%s ELB' % tag_prefix,
                GroupName=kitchen_door_name,
                VpcId=vpc_id,
                DryRun=dry_run)
            kitchen_door_sg_id = resp['GroupId']
            LOGGER.info("%s created %s security group %s",
                tag_prefix, kitchen_door_name, kitchen_door_sg_id)

        try:
            resp = ec2_client.authorize_security_group_ingress(
                DryRun=dry_run,
                GroupId=kitchen_door_sg_id,
                CidrIp='%s/32' % sally_ip,
                IpProtocol='tcp',
                FromPort=22,
                ToPort=22)
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'InvalidPermission.Duplicate':
                raise
        try:
            resp = ec2_client.authorize_security_group_egress(
                DryRun=dry_run,
                GroupId=kitchen_door_sg_id,
                IpPermissions=[{
                    'IpProtocol': '-1',
                    'IpRanges': [{
                        'CidrIp': '0.0.0.0/0',
                    }]}])
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'InvalidPermission.Duplicate':
                raise
        try:
            resp = ec2_client.authorize_security_group_ingress(
                DryRun=dry_run,
                GroupId=gate_sg_id,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': 22,
                    'ToPort': 22,
                    'UserIdGroupPairs': [{'GroupId': kitchen_door_sg_id}]
                }])
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'InvalidPermission.Duplicate':
                raise
        try:
            resp = ec2_client.authorize_security_group_ingress(
                DryRun=dry_run,
                GroupId=vault_sg_id,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': 22,
                    'ToPort': 22,
                    'UserIdGroupPairs': [{'GroupId': kitchen_door_sg_id}]
                }])
        except botocore.exceptions.ClientError as err:
            if not err.response.get('Error', {}).get(
                    'Code', 'Unknown') == 'InvalidPermission.Duplicate':
                raise



def create_datastores(region_name, vpc_cidr, dbs_zone_names,
                      db_master_user, db_master_password,
                      tag_prefix, kms_key_arn=None):
    """
    This function creates in a specified AWS region the disk storage (S3) and
    databases (SQL) to run a SaaS product. It will:

    - create S3 buckets for media uploads and write-only logs
    - create a SQL database
    """
    LOGGER.info("Provisions datastores ...")
    _, dbs_subnet_cidrs = _split_cidrs(vpc_cidr)

    ec2_client = boto3.client('ec2', region_name=region_name)
    vpc_id = _get_vpc_id(tag_prefix, ec2_client=ec2_client)

    vault_name = '%s-vault' % tag_prefix
    group_ids = _get_security_group_ids(
        [vault_name], tag_prefix, vpc_id=vpc_id, ec2_client=ec2_client)
    vault_sg_id = group_ids[0]

    dbs_subnet_by_zones = _get_subnet_by_zones(dbs_subnet_cidrs,
        tag_prefix, vpc_id=vpc_id, ec2_client=ec2_client)

    if not kms_key_arn:
        kms_key_arn = _get_storage_encryption_key(region_name, tag_prefix)

    rds_client = boto3.client('rds', region_name=region_name)
    db_param_group_name = tag_prefix
    try:
        # aws rds describe-db-engine-versions --engine postgres \
        #     --query "DBEngineVersions[].DBParameterGroupFamily"
        rds_client.create_db_parameter_group(
            DBParameterGroupName=db_param_group_name,
            DBParameterGroupFamily='postgres9.6',
            Description='%s parameter group for postgres9.6' % tag_prefix,
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s-db-parameter-group" % tag_prefix}])
        rds_client.modify_db_parameter_group(
            DBParameterGroupName=db_param_group_name,
            Parameters=[{
                'ParameterName': "rds.force_ssl",
                'ParameterValue': "1",
                'ApplyMethod': "pending-reboot"}])
        LOGGER.info("%s created rds db parameter group '%s'",
                    tag_prefix, db_param_group_name)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'DBParameterGroupAlreadyExists':
            raise
        LOGGER.info("%s found rds db parameter group '%s'",
                    tag_prefix, db_param_group_name)

    db_subnet_group_name = tag_prefix
    try:
        resp = rds_client.create_db_subnet_group(
            DBSubnetGroupName=db_subnet_group_name,
            SubnetIds=list(dbs_subnet_by_zones.values()),
            DBSubnetGroupDescription='%s db subnet group' % tag_prefix,
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s-db-subnet-group" % tag_prefix}])
        LOGGER.info("%s created rds db subnet group '%s'",
                    tag_prefix, db_subnet_group_name)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'DBSubnetGroupAlreadyExists':
            raise
        LOGGER.info("%s found rds db subnet group '%s'",
                    tag_prefix, db_subnet_group_name)

    db_name = tag_prefix
    try:
        resp = rds_client.create_db_instance(
            DBName=db_name,
            DBInstanceIdentifier=tag_prefix,
            AllocatedStorage=20,
            DBInstanceClass='db.t3.medium',
            Engine='postgres',
            # aws rds describe-db-engine-versions --engine postgres
            EngineVersion='9.6.14',
            MasterUsername=db_master_user,
            MasterUserPassword=db_master_password,
            VpcSecurityGroupIds=[vault_sg_id],
            AvailabilityZone=dbs_zone_names[0],
            DBSubnetGroupName=db_subnet_group_name,
            DBParameterGroupName=db_param_group_name,
            BackupRetentionPeriod=30,
            #XXX? CharacterSetName='string',
            #StorageType='string', defaults to 'gp2'
            StorageEncrypted=True,
            KmsKeyId=kms_key_arn,
            #XXX MonitoringInterval=123,
            #XXX MonitoringRoleArn='string',
            #XXX EnableIAMDatabaseAuthentication=True|False,
            #XXX EnablePerformanceInsights=True|False,
            #XXX PerformanceInsightsKMSKeyId='string',
            #XXX PerformanceInsightsRetentionPeriod=123,
            #XXX EnableCloudwatchLogsExports=['string'],
            #XXX DeletionProtection=True|False,
            #XXX MaxAllocatedStorage=123
            Tags=[
                {'Key': "Prefix", 'Value': tag_prefix},
                {'Key': "Name", 'Value': "%s-db" % tag_prefix}])
        LOGGER.info("%s created rds db '%s'", tag_prefix, db_name)
    except botocore.exceptions.ClientError as err:
        if not err.response.get('Error', {}).get(
                'Code', 'Unknown') == 'DBInstanceAlreadyExists':
            raise
        LOGGER.info("%s found rds db '%s'", tag_prefix, db_name)


def create_target_group(region_name, app_id,
                        tls_priv_key, tls_fullchain_cert, instance_id,
                        vpc_id=None, listener_arn=None, tag_prefix=None):
    """
    Create TargetGroup to forward HTTPS requests to application service.
    """
    if not vpc_id:
        vpc_id = _get_vpc_id(tag_prefix)

    # We attach the certificate to the load balancer listener
    resp = _store_certificate(tls_fullchain_cert, tls_priv_key,
        tag_prefix=tag_prefix, region_name=region_name)
    valid_domains = (resp['ssl_certificate']['common_name']
        + resp['ssl_certificate']['alt_names'])
    cert_location = resp['CertificateArn']

    elb_client = boto3.client('elbv2', region_name=region_name)
    if not listener_arn:
        listener_arn = _get_listener(
            tag_prefix, elb_client=elb_client)

    # We add the certificate matching the domain such that we can answer
    # requests for the domain over https.
    resp = elb_client.add_listener_certificates(
        ListenerArn=listener_arn,
        Certificates=[{'CertificateArn': cert_location}])

    # We create a listener rule to forward https requests to the app.
    resp = elb_client.create_target_group(
        Name=app_id,
        Protocol='HTTPS',
        Port=443,
        VpcId=vpc_id,
        TargetType='instance')
    target_group = resp.get('TargetGroups')[0].get('TargetGroupArn')

    rule_arn = None
    resp = elb_client.describe_rules(ListenerArn=listener_arn)
    for rule in resp['Rules']:
        if rule['Conditions']['Field'] == 'host-header':
            rule_valid_domains = [] + valid_domains
            for rule_domain in rule['Conditions']['HostHeaderConfig']['Values']:
                rule_valid_domains.remove(rule_domain)
            if not rule_valid_domains:
                rule_arn = rule['RuleArn']
                break
    if rule_arn:
        LOGGER.info("%s found matching listener rule %s",
            tag_prefix, rule_arn)
    else:
        priority = 1
        for rule in resp['Rules']:
            if rule['Priority'] >= priority:
                priority = rule['Priority'] + 1
        resp = elb_client.create_rule(
            ListenerArn=listener_arn,
            Priority=priority,
            Conditions=[
                {
                    'Field': 'host-header',
                    'HostHeaderConfig': {
                        'Values': valid_domains
                    }
                }],
            Actions=[
                {
                    'Type': 'forward',
                    'TargetGroupArn': target_group,
                }
            ])
        rule_arn = resp['RuleArn']
        LOGGER.info("%s created matching listener rule %s",
            tag_prefix, rule_arn)

    # It is time to attach the instance that will respond to http requests
    # to the target group.
    resp = elb_client.register_targets(
        TargetGroupArn=target_group,
        Targets=[{
            'Id': instance_id, # XXX have to wait before registring target?
            'Port': 443
        }])
    LOGGER.info("%s registered instance %s with listener rule %s",
                tag_prefix, instance_id, rule_arn)


def main(input_args):
    """
    Main entry point to run creation of AWS resources
    """
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--dry-run', action='store_true',
        default=False,
        help='Do not create resources')
    parser.add_argument(
        '--prefix', action='store',
        default=None,
        help='prefix used to tag the resources created.')
    parser.add_argument(
        '--config', action='store',
        default=os.path.join(os.getenv('HOME'), '.aws', 'djaoapp'),
        help='configuration file')

    args = parser.parse_args(input_args[1:])
    config = configparser.ConfigParser()
    params = config.read(args.config)
    LOGGER.info("read configuration from %s", args.config)
    for section in config.sections():
        LOGGER.info("[%s]", section)
        for key, val in config.items(section):
            LOGGER.info("%s = %s", key, val)

    tls_priv_key = None
    tls_fullchain_cert = None
    tls_priv_key_path = config['default']['tls_priv_key_path']
    tls_fullchain_path = config['default']['tls_fullchain_path']
    if tls_priv_key_path and tls_fullchain_path:
        with open(tls_priv_key_path) as priv_key_file:
            tls_priv_key = priv_key_file.read()
        with open(tls_fullchain_path) as fullchain_file:
            tls_fullchain_cert = fullchain_file.read()

    ssh_key_name = config['default']['ssh_key_name']
    with open(os.path.join(os.getenv('HOME'), '.ssh', '%s.pub' % ssh_key_name),
              'rb') as ssh_key_obj:
        ssh_key_content = ssh_key_obj.read()

    db_zone_names = [zone_name.strip()
        for zone_name in config['default']['dbs_zone_names'].split(',')]
    create_network(
        config['default']['region_name'],
        config['default']['vpc_cidr'],
        db_zone_names,
        tls_priv_key=tls_priv_key,
        tls_fullchain_cert=tls_fullchain_cert,
        ssh_key_name=ssh_key_name,
        ssh_key_content=ssh_key_content,
        sally_ip=config['default']['sally_ip'],
        tag_prefix=args.prefix,
        dry_run=args.dry_run)

    create_datastores(
        config['default']['region_name'],
        config['default']['vpc_cidr'],
        db_zone_names,
        config['default']['db_master_user'],
        config['default']['db_master_password'],
        args.prefix)

    # Create target groups for the applications.
    for app_name in config:
        if app_name == 'default':
            continue
        tls_priv_key_path = config[app_name]['tls_priv_key_path']
        tls_fullchain_path = config[app_name]['tls_fullchain_path']
        with open(tls_priv_key_path) as priv_key_file:
            tls_priv_key = priv_key_file.read()
        with open(tls_fullchain_path) as fullchain_file:
            tls_fullchain_cert = fullchain_file.read()
        create_target_group(
            config['default']['region_name'],
            app_name,
            tls_priv_key_path,
            tls_fullchain_path,
            config[app_name]['instance_id'],
            args.prefix)