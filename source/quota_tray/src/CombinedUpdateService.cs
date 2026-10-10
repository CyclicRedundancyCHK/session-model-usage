using System;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;

namespace CodexQuotaTray
{
    internal sealed class CombinedUpdateResult
    {
        internal string Chinese;
        internal string English;
        internal string Url;
        internal bool Newer;
    }
    internal static class CombinedUpdateService
    {
        internal const string RepositoryUrl = "https://github.com/CyclicRedundancyCHK/session-model-usage";
        internal static string CurrentVersionLabel
        {
            get
            {
                var assembly = Assembly.GetExecutingAssembly();
                var metadata = (AssemblyInformationalVersionAttribute)Attribute.GetCustomAttribute(assembly, typeof(AssemblyInformationalVersionAttribute));
                return metadata == null ? assembly.GetName().Version.ToString(3) : metadata.InformationalVersion;
            }
        }
        internal static CombinedUpdateResult Parse(string text)
        {
            var data = CompanionBridge.Parse(text);
            var tag = CompanionBridge.Text(CompanionBridge.Get(data, "tag_name"));
            var page = CompanionBridge.Text(CompanionBridge.Get(data, "html_url"));
            if (data == null || CompanionBridge.Get(data, "draft") is bool && (bool)CompanionBridge.Get(data, "draft") ||
                tag == null || !Regex.IsMatch(tag, @"^v\d+\.\d+\.\d+(?:-local\.\d+)?$") ||
                page != RepositoryUrl + "/releases/tag/" + tag)
                return new CombinedUpdateResult { Chinese = "整合版发布信息不可用", English = "Combined release information is unavailable" };
            var parts = tag.Substring(1).Split('-');
            var version = new Version(parts[0]);
            var currentParts = CurrentVersionLabel.Split('-');
            var current = new Version(currentParts[0]);
            var currentLocalRevision = currentParts.Length == 1 ? 0 : int.Parse(currentParts[1].Substring(6));
            var newer = version > current || version == current && currentLocalRevision > 0 &&
                (parts.Length == 1 || int.Parse(parts[1].Substring(6)) > currentLocalRevision);
            var fullPackage = false;
            foreach (var asset in CompanionBridge.Rows(CompanionBridge.Get(data, "assets")))
            {
                var name = CompanionBridge.Text(CompanionBridge.Get(asset, "name"));
                var url = CompanionBridge.Text(CompanionBridge.Get(asset, "browser_download_url"));
                if (name == "session-model-usage-" + tag + "-windows-x64.zip" &&
                    url == RepositoryUrl + "/releases/download/" + tag + "/" + name) fullPackage = true;
            }
            return new CombinedUpdateResult { Newer = newer && fullPackage, Url = page,
                Chinese = newer && fullPackage ? "发现整合版 " + tag : newer ? "整合版安装包尚未发布" : "当前已是最新整合版本",
                English = newer && fullPackage ? "Combined update " + tag : newer ? "Combined package is not published yet" : "The combined app is up to date" };
        }
        internal static CombinedUpdateResult Check()
        {
            try
            {
                ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
                var request = (HttpWebRequest)WebRequest.Create("https://api.github.com/repos/CyclicRedundancyCHK/session-model-usage/releases/latest");
                request.UserAgent = "CodexSessionUsage/" + CurrentVersionLabel;
                request.Timeout = 6000;
                request.ReadWriteTimeout = 6000;
                request.AllowAutoRedirect = false;
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8)) return Parse(reader.ReadToEnd());
            }
            catch (WebException error)
            {
                var response = error.Response as HttpWebResponse;
                if (response != null && response.StatusCode == HttpStatusCode.NotFound)
                    return new CombinedUpdateResult { Chinese = "尚无整合版正式发布", English = "No combined release is published yet" };
                return new CombinedUpdateResult { Chinese = "无法连接发布服务，请稍后重试", English = "Release service is unavailable; retry later" };
            }
            catch { return new CombinedUpdateResult { Chinese = "发布信息无法识别", English = "Release information is unrecognized" }; }
        }
    }
}
