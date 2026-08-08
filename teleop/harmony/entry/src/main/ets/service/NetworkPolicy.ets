import { url } from '@kit.ArkTS';

export class EndpointValidation {
  valid: boolean = false;
  normalizedUrl: string = '';
  warning: string = '';
  error: string = '';
}

export class NetworkPolicy {
  static validate(rawUrl: string): EndpointValidation {
    const result = new EndpointValidation();
    const endpoint = rawUrl.trim();
    if (endpoint.length === 0) {
      result.error = '请输入 WebSocket 地址';
      return result;
    }

    let parsed: url.URL;
    try {
      parsed = url.URL.parseURL(endpoint);
    } catch (error) {
      result.error = 'WebSocket 地址格式无效';
      return result;
    }

    const scheme = parsed.protocol.toLowerCase();
    if (scheme !== 'ws:' && scheme !== 'wss:') {
      result.error = '地址必须使用 ws:// 或 wss://';
      return result;
    }
    if (parsed.username.length > 0 || parsed.password.length > 0) {
      result.error = '不要把 token 或账号密码写在 URL 中';
      return result;
    }
    if (parsed.hash.length > 0) {
      result.error = 'WebSocket 地址不能包含 fragment';
      return result;
    }
    if (parsed.search.length > 0) {
      result.error = 'WebSocket 地址不能包含 query；token 必须通过 session.hello 发送';
      return result;
    }
    if (parsed.hostname.length === 0) {
      result.error = 'WebSocket 地址缺少主机名';
      return result;
    }

    if (scheme === 'ws:' && !NetworkPolicy.isPrivateOrLocalHost(parsed.hostname)) {
      result.error = '明文 ws:// 仅允许 localhost、.local 或字面量环回/RFC1918/169.254 地址';
      return result;
    }

    result.valid = true;
    result.normalizedUrl = endpoint;
    if (scheme === 'ws:') {
      result.warning = '当前使用局域网明文 WS；生产环境必须切换 WSS';
    }
    return result;
  }

  private static isPrivateOrLocalHost(rawHost: string): boolean {
    let host = rawHost.trim().toLowerCase();
    if (host.startsWith('[') && host.endsWith(']') && host.length > 2) {
      host = host.substring(1, host.length - 1);
    }
    if (host === 'localhost' || host === '::1' || host.endsWith('.local')) {
      return true;
    }
    const parts = host.split('.');
    if (parts.length !== 4) {
      return false;
    }
    const octets: Array<number> = [];
    for (let index = 0; index < parts.length; index += 1) {
      if (parts[index].length === 0) {
        return false;
      }
      const octet = Number(parts[index]);
      if (!Number.isInteger(octet) || octet < 0 || octet > 255) {
        return false;
      }
      octets.push(octet);
    }

    return octets[0] === 10 || octets[0] === 127 ||
      (octets[0] === 169 && octets[1] === 254) ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168);
  }
}
