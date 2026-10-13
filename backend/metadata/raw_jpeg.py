"""Bounded raw JPEG segment archive; entropy is skipped without decoding pixels."""
import base64
import io
import struct
from backend.metadata.jpeg_semantics import parse_jpeg_app1

SOF = {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}


def read_jpeg_headers(source, *, max_total_bytes=8*1024*1024, max_segments=4096,
                      full_scan=False, max_scan_bytes=128*1024*1024):
    if type(max_total_bytes) is not int or max_total_bytes < 0 or type(max_segments) is not int or max_segments < 1:
        raise ValueError('Invalid JPEG archive budgets')
    if type(full_scan) is not bool or type(max_scan_bytes) is not int or max_scan_bytes < 0:
        raise ValueError('Invalid JPEG scan budget')
    stream = io.BytesIO(source) if isinstance(source,bytes) else source
    result = {'status':'error','complete':False,'coverage':'none','segments':[],
              'dimensions':None,'errors':[],'capture_scope':'before_first_scan'}
    if full_scan:
        result['capture_scope'] = 'all_scans_until_first_eoi'
    captured = 0
    offset = 0
    scan_bytes = 0
    class ScanBudgetExceeded(ValueError):
        pass
    def skip_scan():
        nonlocal offset, scan_bytes
        start = offset
        position = offset
        pending = False
        while True:
            if scan_bytes + position - start > max_scan_bytes:
                raise ScanBudgetExceeded('JPEG scan byte budget exceeded')
            chunk = stream.read(65536)
            if not chunk:
                raise EOFError('Truncated JPEG entropy scan')
            data = (b'\xff' + chunk) if pending else chunk
            base = position - 1 if pending else position
            pending = False
            cursor = 0
            while True:
                marker_at = data.find(b'\xff',cursor)
                if marker_at < 0:
                    break
                if marker_at + 1 == len(data):
                    pending = True
                    break
                marker = data[marker_at+1]
                if marker == 0 or 0xD0 <= marker <= 0xD7:
                    cursor = marker_at + 2
                    continue
                if marker == 0xFF:
                    cursor = marker_at + 1
                    continue
                consumed = base + marker_at - start
                if scan_bytes + consumed > max_scan_bytes:
                    raise ScanBudgetExceeded('JPEG scan byte budget exceeded')
                offset = base + marker_at
                scan_bytes += consumed
                stream.seek(offset)
                return
            position += len(chunk)
    def read(size):
        nonlocal offset
        data = bytearray()
        while len(data) < size:
            block = stream.read(size-len(data))
            if not block:
                raise EOFError('Truncated JPEG segment')
            data.extend(block)
        offset += size
        return bytes(data)
    try:
        if read(2) != b'\xff\xd8':
            raise ValueError('Invalid JPEG SOI')
        for _ in range(max_segments):
            segment_offset = offset
            if read(1) != b'\xff':
                raise ValueError('Invalid JPEG marker boundary')
            marker = read(1)[0]
            padding = 0
            while marker == 0xFF:
                padding += 1
                if padding > 65536:
                    raise ValueError('JPEG marker padding exceeds budget')
                marker = read(1)[0]
            if marker == 0xD9:
                result.update(status='complete' if result['segments'] else 'no_metadata',complete=True,coverage='complete')
                if full_scan and stream.read(1):
                    result.update(status='partial',complete=False,coverage='partial')
                    result['errors'].append('Data after the first EOI is not collected')
                return result
            if marker in (0,0xD8) or 0xD0 <= marker <= 0xD7:
                raise ValueError('Unexpected JPEG marker before scan')
            if marker == 0x01:
                continue
            length = struct.unpack('>H',read(2))[0]
            if length < 2:
                raise ValueError('Invalid JPEG segment length')
            payload = read(length-2)
            if 0xE0 <= marker <= 0xEF or marker == 0xFE:
                record = {'offset':segment_offset,'marker':f'FF{marker:02X}','length':length,
                          'raw_payload_b64':None}
                result['segments'].append(record)
                if captured + len(payload) > max_total_bytes:
                    record['capture_error'] = 'JPEG metadata budget exceeded'
                    result.update(status='too_large',coverage='partial')
                    return result
                record['raw_payload_b64'] = base64.b64encode(payload).decode('ascii')
                captured += len(payload)
                if marker == 0xE1:
                    record['semantics'] = parse_jpeg_app1(payload, segment_offset)
                if marker == 0xFE:
                    try:
                        record['text'] = payload.decode('utf-8',errors='strict')
                    except UnicodeDecodeError:
                        record['text'] = None
            if marker in SOF:
                if len(payload) < 6 or payload[5] < 1 or len(payload) != 6 + 3 * payload[5]:
                    raise ValueError('Truncated JPEG frame header')
                height,width = struct.unpack('>HH',payload[1:5])
                if not width or not height:
                    raise ValueError('Deferred or invalid JPEG dimensions')
                dimensions = {'width':width,'height':height,'source':f'SOF{marker:02X}'}
                existing = result['dimensions']
                if existing and (existing['width'],existing['height']) != (width,height):
                    result['dimensions'] = None
                    raise ValueError('Conflicting JPEG frame dimensions')
                result['dimensions'] = dimensions
            if marker == 0xDA:
                if full_scan:
                    if len(payload) < 4 or payload[0] < 1 or len(payload) != 4 + 2 * payload[0]:
                        raise ValueError('Invalid JPEG scan header')
                    skip_scan()
                    continue
                result.update(status='partial',coverage='partial')
                result['errors'].append('Metadata after the first scan is not collected')
                return result
        result.update(status='too_large',coverage='partial')
        result['errors'].append('JPEG segment count exceeds budget')
    except ScanBudgetExceeded as error:
        result.update(status='too_large',coverage='partial')
        result['errors'].append(str(error))
    except (EOFError,ValueError,OSError) as error:
        result.update(status='error',coverage='partial')
        result['errors'].append(str(error))
    return result
