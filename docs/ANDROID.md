# 打包成 Android APK(Capacitor)

项目已备好 `capacitor.config.json`。打包需要在你自己的电脑上进行(需要 Android Studio 和签名),步骤如下。

## 前置条件

- Node.js ≥ 18(打包机器上)
- [Android Studio](https://developer.android.com/studio)(含 SDK)
- 你的服务器已部署本项目并可从手机访问(见下)

## 思路

App 外壳加载你部署的服务器地址(`server.url`),这样后端接口、账号数据、服务端 Key 代理全部直接可用,而且以后更新功能只要更新服务器,**不用重新发版**。

## 步骤

```bash
# 1. 在项目根目录安装 Capacitor(这台机器要能访问 npm)
npm install @capacitor/core @capacitor/cli @capacitor/android

# 2. 修改 capacitor.config.json 里的 server.url
#    改成你部署的地址,例如 https://map.example.com
#    (生产务必用 https,并删掉 "cleartext": true)

# 3. 生成 Android 工程
npx cap add android

# 4. 同步
npx cap sync android

# 5. 打开 Android Studio 构建
npx cap open android
#    Build → Generate Signed Bundle / APK → 按向导创建签名密钥 → 生成 APK
```

## 权限

生成工程后,编辑 `android/app/src/main/AndroidManifest.xml`,确认包含定位权限:

```xml
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<uses-permission android:name="android.permission.INTERNET" />
```

实况记录若需熄屏继续,可再加 `FOREGROUND_SERVICE`(需要额外原生代码,或先提示用户保持亮屏)。

## 高德 Key 说明

- 推荐在管理后台(`/admin.html`)配置服务端 Key,App 内所有人直接可用,无需各自填 Key。
- 高德控制台里给这个 Key 加上你的服务器域名白名单更安全。

## 不想装 Android Studio?

也可以直接用手机浏览器打开部署地址 → “添加到主屏幕”(PWA),体验已非常接近原生 App,这条路零成本。
