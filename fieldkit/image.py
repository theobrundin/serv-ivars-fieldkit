"""Tapo C113 image controls, with readback and rollback on rejected changes."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from xml.etree import ElementTree as E
from xml.sax.saxutils import escape

from fieldkit.host import CAMERA, private_json, run

NS = {'trt': 'http://www.onvif.org/ver10/media/wsdl',
      'timg': 'http://www.onvif.org/ver20/imaging/wsdl', 'tt': 'http://www.onvif.org/ver10/schema'}
CONTROLS = {'brightness': 'Brightness', 'contrast': 'Contrast',
            'saturation': 'ColorSaturation', 'sharpness': 'Sharpness'}


def call(prefix, operation, body=''):
    import requests
    route = json.loads(run(['ip', '-j', '-4', 'route', 'get', CAMERA])[1])[0]
    if route.get('dev') != 'wlan0' or route.get('gateway'):
        raise ValueError('Camera is not on the isolated local link')
    c = private_json('/home/serv/tapo-bench/camera-credentials.json')
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    nonce = os.urandom(16)
    digest = base64.b64encode(hashlib.sha1(nonce + stamp.encode() + c['password'].encode()).digest()).decode()
    wsse = 'http://docs.oasis-open.org/wss/2004/01/'
    text = f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:{prefix}="{NS[prefix]}" xmlns:tt="{NS['tt']}"><s:Header><w:Security s:mustUnderstand="1" xmlns:w="{wsse}oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:u="{wsse}oasis-200401-wss-wssecurity-utility-1.0.xsd"><w:UsernameToken><w:Username>{escape(c['username'])}</w:Username><w:Password Type="{wsse}oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</w:Password><w:Nonce EncodingType="{wsse}oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</w:Nonce><u:Created>{stamp}</u:Created></w:UsernameToken></w:Security></s:Header><s:Body><{prefix}:{operation}>{body}</{prefix}:{operation}></s:Body></s:Envelope>'''
    with requests.Session() as session:
        session.trust_env = False
        response = session.post('http://' + CAMERA + ':2020/onvif/service', data=text.encode(),
            headers={'Content-Type': f'application/soap+xml; charset=utf-8; action="{NS[prefix]}/{operation}"'}, timeout=6)
        xml = E.fromstring(response.content)
        if xml.find('.//{http://www.w3.org/2003/05/soap-envelope}Fault') is not None:
            raise ValueError('Camera rejected the ONVIF command')
        response.raise_for_status()
        return xml


def settings(source):
    result = call('timg', 'GetImagingSettings', source).find('.//timg:ImagingSettings', NS)
    if result is None:
        raise ValueError('Camera returned no image settings')
    return result


def summarize(xml):
    return {key: float(xml.find('tt:' + tag, NS).text) for key, tag in CONTROLS.items()
            if xml.find('tt:' + tag, NS) is not None}


def adjust(changes):
    for key, value in changes.items():
        if key not in CONTROLS or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError('Image controls must be integers from 0 through 100')
    token = call('trt', 'GetVideoSources').find('.//trt:VideoSources', NS).attrib['token']
    source = '<timg:VideoSourceToken>' + escape(token) + '</timg:VideoSourceToken>'
    before = settings(source)
    if not changes:
        return summarize(before)
    changed = copy.deepcopy(before)
    for key, value in changes.items():
        node = changed.find('tt:' + CONTROLS[key], NS)
        if node is None:
            raise ValueError('Camera does not expose ' + key)
        node.text = str(value)
    def put(value):
        call('timg', 'SetImagingSettings', source + E.tostring(value, encoding='unicode') + '<timg:ForcePersistence>true</timg:ForcePersistence>')
    try:
        put(changed)
        result = summarize(settings(source))
        if any(result.get(k) != v for k, v in changes.items()):
            raise ValueError('Camera ignored the requested setting')
    except Exception:
        put(before)
        if summarize(settings(source)) != summarize(before):
            raise ValueError('Image-control rollback was not confirmed')
        raise
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for key in CONTROLS:
        p.add_argument('--' + key, type=int)
    a = p.parse_args()
    try:
        print(json.dumps(adjust({k: v for k, v in vars(a).items() if v is not None}), indent=2))
    except Exception as e:
        print(str(e) if isinstance(e, ValueError) else 'Camera request failed; private details suppressed', file=sys.stderr)
        sys.exit(1)
