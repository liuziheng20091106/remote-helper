using System.Diagnostics;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;
using Avalonia.Threading;
using ClassIsland.Core;
using ClassIsland.Core.Abstractions.Services;
using ClassIsland.Core.Abstractions.Services.Management;
using ClassIsland.Core.Abstractions.Services.NotificationProviders;
using ClassIsland.Core.Attributes;
using ClassIsland.Core.Models.Notification;
using ClassIsland.Shared;
using ClassIsland.Shared.Enums;
using ClassIsland.Shared.Models.Profile;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using NotificationRequest = ClassIsland.Core.Models.Notification.NotificationRequest;
using NotificationSettings = ClassIsland.Shared.Models.Notification.NotificationSettings;

namespace RemoteHelper.ClassIsland;

[NotificationProviderInfo("52319e4a-93b2-43a4-823b-6919c87a2881", "Remote Helper", "远程管理提醒")]
[NotificationChannelInfo(ChannelId, "Remote Helper 专属提醒", description: "由 Remote Helper 网页发出的提醒")]
public sealed class RemoteControlService : NotificationProviderBase, IHostedService
{
    public const string ChannelId = "c0d8182b-0e6d-4638-8398-402f30fdf91a";
    private readonly IProfileService _profiles;
    private readonly ILessonsService _lessons;
    private readonly IExactTimeService _clock;
    private readonly IManagementService _management;
    private readonly INotificationHostService _notifications;
    private readonly ILogger<RemoteControlService> _logger;
    private readonly CancellationTokenSource _stop = new();
    private readonly List<NotificationRequest> _requests = new();
    private Task? _server;
    private ClassPlan? _observedPlan;
    private string _planToken = "";
    private DateTime _observedDate;
    private (object Item, TimeSpan Start, TimeSpan End, Guid? Subject)[] _planMapping = [];

    public RemoteControlService(IProfileService profiles, ILessonsService lessons, IExactTimeService clock,
        IManagementService management, INotificationHostService notifications, ILogger<RemoteControlService> logger)
    {
        _profiles = profiles;
        _lessons = lessons;
        _clock = clock;
        _management = management;
        _notifications = notifications;
        _logger = logger;
    }

    public new Task StartAsync(CancellationToken cancellationToken)
    {
        if (OperatingSystem.IsWindows()) _server = Task.Run(ServeAsync);
        _logger.LogInformation("Remote Helper IPC 服务已启动。");
        return Task.CompletedTask;
    }

    public new async Task StopAsync(CancellationToken cancellationToken)
    {
        _stop.Cancel();
        _logger.LogInformation("Remote Helper IPC 服务已停止。");
        void CancelOwnReminders()
        {
            foreach (var request in _requests) request.Cancel();
            _requests.Clear();
        }
        if (Dispatcher.UIThread.CheckAccess()) CancelOwnReminders();
        else Dispatcher.UIThread.Post(CancelOwnReminders);
        if (_server is not null)
        {
            try { await _server.WaitAsync(cancellationToken); }
            catch (OperationCanceledException) { }
        }
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl, uint revision, out IntPtr descriptor, out uint size);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetSecurityDescriptorSacl(IntPtr descriptor, out bool present, out IntPtr sacl, out bool defaulted);

    [DllImport("advapi32.dll")]
    private static extern uint SetSecurityInfo(IntPtr handle, uint objectType, uint securityInfo, IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    private NamedPipeServerStream CreatePipe()
    {
        var sid = WindowsIdentity.GetCurrent().User ?? throw new InvalidOperationException("无法获取 Windows 用户。");
        var security = new PipeSecurity();
        security.SetAccessRuleProtection(true, false);
        // Keep the DACL user-scoped; UAC elevation does not change the user's SID.
        security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.NetworkSid, null), PipeAccessRights.FullControl, AccessControlType.Deny));
        security.AddAccessRule(new PipeAccessRule(sid, PipeAccessRights.FullControl, AccessControlType.Allow));
        var name = $"RemoteHelper.ClassIsland21.{sid.Value}.{Process.GetCurrentProcess().SessionId}";
        var pipe = NamedPipeServerStreamAcl.Create(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous, 65536, 65536, security);
        IntPtr descriptor = IntPtr.Zero;
        try
        {
            // .NET's SystemAcl drops mandatory-label ACEs. Set the native label
            // explicitly so a medium-integrity helper can write to an elevated host.
            if (!ConvertStringSecurityDescriptorToSecurityDescriptorW("S:(ML;;NW;;;ME)", 1, out descriptor, out _) ||
                !GetSecurityDescriptorSacl(descriptor, out var present, out var sacl, out _) || !present)
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            var result = SetSecurityInfo(pipe.SafePipeHandle.DangerousGetHandle(), 6, 0x10, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, sacl);
            if (result != 0) throw new System.ComponentModel.Win32Exception((int)result);
            return pipe;
        }
        catch { pipe.Dispose(); throw; }
        finally { if (descriptor != IntPtr.Zero) LocalFree(descriptor); }
    }

    private async Task ServeAsync()
    {
        try
        {
            await using var pipe = CreatePipe();
            while (!_stop.IsCancellationRequested)
            {
                await pipe.WaitForConnectionAsync(_stop.Token);
                using var deadline = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
                deadline.CancelAfter(TimeSpan.FromSeconds(4));
                try
                {
                    object response;
                    try
                    {
                        var buffer = new byte[16384];
                        var used = 0;
                        while (true)
                        {
                            if (used == buffer.Length) throw new ArgumentException("请求超过 16 KiB。");
                            var size = await pipe.ReadAsync(buffer.AsMemory(used), deadline.Token);
                            if (size == 0) throw new IOException("请求未完成。");
                            used += size;
                            if (buffer[used - 1] == (byte)'\n') break;
                        }
                        using var document = JsonDocument.Parse(buffer.AsMemory(0, used));
                        var data = await Dispatcher.UIThread.InvokeAsync(() => Execute(document.RootElement), DispatcherPriority.Normal, deadline.Token);
                        response = new { ok = true, data };
                    }
                    catch (ArgumentException error) { response = new { ok = false, kind = "invalid", error = error.Message }; }
                    catch (JsonException) { response = new { ok = false, kind = "invalid", error = "请求 JSON 无效。" }; }
                    catch (Exception error) when (error is not OperationCanceledException && error is not IOException)
                    {
                        _logger.LogWarning("Remote Helper 操作失败: {Type}", error.GetType().Name);
                        response = new { ok = false, kind = "unavailable", error = error.InnerException?.Message ?? error.Message };
                    }
                    var bytes = JsonSerializer.SerializeToUtf8Bytes(response);
                    if (bytes.Length > 256 * 1024) throw new IOException("响应过大。");
                    await pipe.WriteAsync(bytes, deadline.Token);
                    await pipe.WriteAsync(new byte[] { (byte)'\n' }, deadline.Token);
                    await pipe.FlushAsync(deadline.Token);
                    // Let the client drain its response and close before disconnecting.
                    await pipe.ReadAsync(new byte[1], deadline.Token);
                }
                catch (OperationCanceledException) { }
                catch (IOException) { }
                finally { pipe.Disconnect(); }
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception error) { _logger.LogError(error, "Remote Helper 管道不可用。"); }
    }

    private static object SettingsObject()
    {
        // 2.1 SDK does not expose the app settings service. Use its existing properties,
        // never private-field mutation; version checks below deliberately exclude 2.2.
        var type = AppBase.Current.GetType().Assembly.GetType("ClassIsland.Services.SettingsService", true)!;
        var service = IAppHost.Host?.Services.GetService(type) ?? throw new InvalidOperationException("设置服务尚未就绪。");
        return type.GetProperty("Settings")!.GetValue(service)!;
    }

    private static PropertyInfo Setting(object settings, string name) => settings.GetType().GetProperty(name)
        ?? throw new InvalidOperationException($"ClassIsland 2.1 缺少设置 {name}。");

    private static string Text(JsonElement request, string key, int maximum)
    {
        if (request.ValueKind != JsonValueKind.Object || !request.TryGetProperty(key, out var value) || value.ValueKind != JsonValueKind.String ||
            string.IsNullOrWhiteSpace(value.GetString()) || value.GetString()!.Length > maximum)
            throw new ArgumentException($"{key} 无效。");
        return value.GetString()!;
    }

    private static bool Boolean(JsonElement request, string key)
    {
        if (!request.TryGetProperty(key, out var value) || value.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            throw new ArgumentException($"{key} 必须为布尔值。");
        return value.GetBoolean();
    }

    private static void Properties(JsonElement request, params string[] fields)
    {
        if (request.ValueKind != JsonValueKind.Object || request.EnumerateObject().Any(p => p.Name != "action" && !fields.Contains(p.Name)))
            throw new ArgumentException("请求包含不支持的参数。");
    }

    private void RequireUnprotected(AuthorizeLevel level)
    {
        var credentials = _management.CredentialConfig;
        if (level != AuthorizeLevel.None && (!string.IsNullOrEmpty(credentials.AdminCredential) || !string.IsNullOrEmpty(credentials.UserCredential)))
            throw new InvalidOperationException("此操作受 ClassIsland 授权保护，请在电脑端使用原生界面完成；插件不会绕过授权。");
    }

    private ClassPlan? CurrentPlan(out Guid? id) => _lessons.IsClassPlanEnabled
        ? _lessons.GetClassPlanByDate(_clock.GetCurrentLocalDateTime(), out id)
        : NoPlan(out id);

    private static ClassPlan? NoPlan(out Guid? id) { id = null; return null; }

    private object State()
    {
        var settings = SettingsObject();
        var plan = CurrentPlan(out _);
        var date = _clock.GetCurrentLocalDateTime().Date;
        var profile = _profiles.Profile;
        var periods = plan is not null && profile.TimeLayouts.TryGetValue(plan.TimeLayoutId, out var layout)
            ? layout.Layouts.Where(item => item.TimeType == 0).ToList() : [];
        var mapping = periods.Select((period, index) => ((object)period, period.StartTime, period.EndTime, plan?.Classes.ElementAtOrDefault(index)?.SubjectId)).ToArray();
        if (!ReferenceEquals(plan, _observedPlan) || date != _observedDate || !_planMapping.SequenceEqual(mapping))
        {
            _observedPlan = plan;
            _observedDate = date;
            _planMapping = mapping;
            _planToken = Guid.NewGuid().ToString("N");
        }
        var lessons = plan?.Classes.Take(periods.Count).Select((lesson, index) => new
        {
            index, subject_id = lesson.SubjectId.ToString(),
            subject = profile.Subjects.TryGetValue(lesson.SubjectId, out var subject) ? subject.Name : "未设置科目",
            start = periods[index].StartTime.ToString(@"hh\:mm"), end = periods[index].EndTime.ToString(@"hh\:mm")
        }).ToArray();
        _requests.RemoveAll(request => request.CompletedToken.IsCancellationRequested || request.CancellationToken.IsCancellationRequested);
        return new
        {
            connected = true, version = $"{AppBase.AppVersion} · {AppBase.AppCodeName}",
            host_pid = Environment.ProcessId,
            host_elevated = new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator),
            time_offset_seconds = (double)Setting(settings, "TimeOffsetSeconds").GetValue(settings)!,
            visible = (bool)Setting(settings, "IsMainWindowVisible").GetValue(settings)!,
            reminders_playing = _notifications.IsNotificationsPlaying,
            channel_id = ChannelId, pending_remote_reminders = _requests.Count,
            plan_id = plan is null ? "" : _planToken, plan_name = plan?.Name ?? "", lessons,
            subjects = profile.Subjects.Select(pair => new { id = pair.Key.ToString(), name = pair.Value.Name }).ToArray()
        };
    }

    private object Execute(JsonElement request)
    {
        var version = typeof(AppBase).Assembly.GetName().Version!;
        if (version.Major != 2 || version.Minor != 1 || AppBase.AppCodeName != "LiliyaOlenyeva")
            throw new InvalidOperationException("仅支持 ClassIsland 2.1 LiliyaOlenyeva，未适配 2.2。");
        if (AppBase.Current.MainWindow is null) throw new InvalidOperationException("ClassIsland 正在启动，请稍后刷新。");
        var action = Text(request, "action", 40);
        switch (action)
        {
            case "state": Properties(request); break;
            case "set_time_offset":
                Properties(request, "seconds");
                if (!request.TryGetProperty("seconds", out var offset) || offset.ValueKind != JsonValueKind.Number || !offset.TryGetDouble(out var seconds) || !double.IsFinite(seconds) || Math.Abs(seconds) > 86400)
                    throw new ArgumentException("偏移必须为 -86400 到 86400 之间的秒数。");
                if (_management.Policy.DisableSettingsEditing) throw new InvalidOperationException("ClassIsland 已禁止编辑设置。");
                RequireUnprotected(_management.CredentialConfig.EditSettingsAuthorizeLevel);
                var timeSettings = SettingsObject();
                Setting(timeSettings, "TimeOffsetSeconds").SetValue(timeSettings, seconds);
                break;
            case "change_lesson":
                Properties(request, "plan_id", "index", "subject_id");
                _ = State(); // Refresh live identity before checking an old browser request.
                var token = Text(request, "plan_id", 128);
                var plan = CurrentPlan(out var id);
                if (plan is null || id is null || token != _planToken)
                    throw new InvalidOperationException("课表已切换，请刷新后重新选择课节。");
                if (!request.TryGetProperty("index", out var item) || item.ValueKind != JsonValueKind.Number || !item.TryGetInt32(out var index) || index < 0 || index >= plan.Classes.Count)
                    throw new ArgumentException("课节不存在。");
                if (!Guid.TryParse(Text(request, "subject_id", 128), out var subjectId) || !_profiles.Profile.Subjects.ContainsKey(subjectId))
                    throw new ArgumentException("科目不存在，请刷新。");
                RequireUnprotected(_management.CredentialConfig.ChangeLessonsAuthorizeLevel);
                var targetId = plan.IsOverlay ? id : _profiles.CreateTempClassPlan(id.Value);
                if (targetId is null) throw new InvalidOperationException("当天已存在其他临时课表，未覆盖；请在 ClassIsland 中处理后刷新。");
                _profiles.Profile.ClassPlans[targetId.Value].Classes[index].SubjectId = subjectId;
                _profiles.SaveProfile();
                break;
            case "set_visible":
                Properties(request, "visible");
                var visible = Boolean(request, "visible");
                var visibilitySettings = SettingsObject();
                Setting(visibilitySettings, "IsMainWindowVisible").SetValue(visibilitySettings, visible);
                break;
            case "clear_notifications":
                Properties(request);
                // The runtime's public method backs the tray action; the SDK interface
                // marks it internal in 2.1. Fail explicitly if that exact API changes.
                var clear = _notifications.GetType().GetMethod("CancelAllNotifications", BindingFlags.Public | BindingFlags.Instance)
                    ?? throw new InvalidOperationException("ClassIsland 2.1 清除提醒接口不可用。");
                clear.Invoke(_notifications, null);
                break;
            case "notify":
                Properties(request, "title", "message", "duration_seconds", "sound");
                var title = Text(request, "title", 100);
                var message = Text(request, "message", 2000);
                if (!request.TryGetProperty("duration_seconds", out var duration) || duration.ValueKind != JsonValueKind.Number || !duration.TryGetInt32(out var durationSeconds) || durationSeconds < 1 || durationSeconds > 60)
                    throw new ArgumentException("提醒显示时长必须为 1 到 60 秒。");
                var sound = Boolean(request, "sound");
                _requests.RemoveAll(r => r.CompletedToken.IsCancellationRequested || r.CancellationToken.IsCancellationRequested);
                if (_requests.Count >= 32) throw new InvalidOperationException("远程提醒队列已满，请等待或清除提醒。");
                var notification = new NotificationRequest
                {
                    MaskContent = NotificationContent.CreateTwoIconsMask(title, hasRightIcon: false, factory: content => content.Duration = TimeSpan.FromSeconds(1)),
                    OverlayContent = NotificationContent.CreateSimpleTextContent(message, content => content.Duration = TimeSpan.FromSeconds(durationSeconds)),
                    RequestNotificationSettings = new NotificationSettings { IsSettingsEnabled = true, IsNotificationSoundEnabled = sound }
                };
                Channel(ChannelId).ShowNotification(notification);
                _requests.Add(notification);
                break;
            default: throw new ArgumentException("不支持的 ClassIsland 操作。");
        }
        return State();
    }
}
