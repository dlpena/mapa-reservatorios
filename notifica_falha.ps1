# Mostra uma notificação (toast) do Windows quando a publicação do mapa falha.
# Chamado pelo publica_mapa.bat; se o toast não puder ser exibido (sessão sem
# desktop, etc.), falha em silêncio — o registro em logs\ continua valendo.
param([string]$Mensagem = "Falha na atualizacao do mapa de reservatorios. Ver logs na pasta do repo.")

try {
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

    $xml = @"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>Mapa de Reservatorios</text>
      <text>$Mensagem</text>
    </binding>
  </visual>
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification($doc)
    # AppId do PowerShell: aparece como remetente da notificação
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
} catch {
    # sem desktop interativo ou WinRT indisponível: nada a fazer
}
