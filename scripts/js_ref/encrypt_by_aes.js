// 官方 login.js 里的 encryptByAES —— 逐字抄自
// https://passport2-static.chaoxing.com/js/fanya/login.js?v=20260908
// 仅用于和 Python 实现做跨实现对照，不参与线上流程。
const CryptoJS = require('crypto-js');

function encryptByAES(message, key) {
  let CBCOptions = {
    iv: CryptoJS.enc.Utf8.parse(key),
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7
  };
  let aeskey = CryptoJS.enc.Utf8.parse(key);
  let secretData = CryptoJS.enc.Utf8.parse(message);
  let encrypted = CryptoJS.AES.encrypt(
    secretData,
    aeskey,
    CBCOptions
  );
  return CryptoJS.enc.Base64.stringify(encrypted.ciphertext);
}

const transferKey = "u2oh6Vu^HWe4_AES";
const samples = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(samples.map(function (s) { return encryptByAES(s, transferKey); })));
