# 前端第三方资源（本地化）

应用运行时不依赖任何 CDN：以下资源随仓库分发，离线可用。
（导出的报告 HTML 是发给别人看的，`report.html` 会在导出时把这些路径换回 CDN。）

| 文件 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| bootstrap.min.css | Bootstrap 5.3.2 | MIT | https://github.com/twbs/bootstrap |
| bootstrap.bundle.min.js | Bootstrap 5.3.2 | MIT | 同上（内含 @popperjs/core，MIT） |
| jquery.min.js | jQuery 3.7.1 | MIT | https://github.com/jquery/jquery |
| echarts.min.js | ECharts 5.6.0 | Apache-2.0 | https://github.com/apache/echarts |
| echarts-wordcloud.min.js | echarts-wordcloud 2.1.0 | MIT | https://github.com/ecomfe/echarts-wordcloud |

各文件头部自带许可证注释；`echarts-wordcloud.min.js.LICENSE.txt` 是其打包的
wordcloud2.js 许可证附件（构建产物内声明，文件名必须保持原样）。

获取方式（升级时重新下载并核对版本）：

```bash
curl -o bootstrap.min.css           https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css
curl -o bootstrap.bundle.min.js     https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js
curl -o jquery.min.js               https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js
curl -o echarts.min.js              https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js
curl -o echarts-wordcloud.min.js    https://cdn.jsdelivr.net/npm/echarts-wordcloud@2.1.0/dist/echarts-wordcloud.min.js
```

## SRI（分享版报告用）

导出的报告 HTML 没有本机服务端，`report.html` 会把上面的本地路径换回 jsDelivr，并带上
`integrity`（sha384）与 `crossorigin="anonymous"`；哈希表就是该模板里的 `VENDOR_CDN`。

```bash
python tools/verify_vendor_sri.py            # 联网：本地文件 <-> CDN 实际字节 <-> 内联常量
python tools/verify_vendor_sri.py --offline  # 离线：只比对本地文件与内联常量
python tools/verify_vendor_sri.py --print    # 离线：按本地文件打印要贴回模板的哈希表
```

`tests/test_sri.py` 在每次跑测试时用本地文件重算 sha384 与内联常量逐条比对，所以「改了文件忘了
改哈希」会当场变红。但它证明不了「本地文件 == CDN 实际提供的字节」——那是个需要联网的历史事实，
不是永久保证：SRI 比的是浏览器真正收到的字节，CDN 上的文件一变而常量没跟着变，浏览器会**静默
拦掉**该资源（样式、图表全没），所以每次升级/重新下载后都要跑一次联网复核。

最近一次核对（2026-09-12）：5 个文件从 `cdn.jsdelivr.net` 重新下载后与本地文件逐字节相同
（sha256 也与 data.jsdelivr.com 公布的一致），模板里的 sha384 常量即取自这批字节。
