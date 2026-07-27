import { useState, useEffect } from 'react';

function App() {
  const [emails, setEmails] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [isLoggedOut, setIsLoggedOut] = useState(false);
  const [isPolling, setIsPolling] = useState(false);
  const [userEmail, setUserEmail] = useState("Admin User");

  const handleLogout = async () => {
    try {
      await fetch('http://localhost:8000/logout', { method: 'POST' });
      setEmails([]);
      setShowUserMenu(false);
      setIsLoggedOut(true);
      alert('Logged out. All records cleared.');
    } catch (error) {
      console.error("Error logging out:", error);
    }
  };

  const handleSwitchAccount = async () => {
    try {
      await fetch('http://localhost:8000/logout', { method: 'POST' });
      setEmails([]);
      setShowUserMenu(false);
      await fetch('http://localhost:8000/authenticate', { method: 'POST' });
      fetchEmails();
    } catch (error) {
      console.error("Error switching account:", error);
    }
  };

  const fetchEmails = async () => {
    try {
      const response = await fetch('http://localhost:8000/emails');
      const data = await response.json();
      
      try {
        const statusResponse = await fetch('http://localhost:8000/status');
        const statusData = await statusResponse.json();
        setIsPolling(statusData.is_polling || false);
        
        const userResponse = await fetch('http://localhost:8000/user');
        const userData = await userResponse.json();
        setUserEmail(userData.email || "Admin User");
      } catch (e) {
        setIsPolling(false);
      }
      
      if (data.status === 'unauthorized') {
        setIsLoggedOut(true);
        setEmails([]);
      } else {
        setIsLoggedOut(false);
        setEmails(data.emails || []);
      }
    } catch (error) {
      console.error("Error fetching emails:", error);
    } finally {
      setLoading(false);
    }
  };

  const handleFeedback = async (email, label) => {
    try {
      await fetch('http://localhost:8000/feedback', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          email_id: email.id,
          subject: email.subject,
          body: email.body,
          label: label
        })
      });
      fetchEmails();
    } catch (error) {
      console.error("Error submitting feedback:", error);
    }
  };

  const handleLogin = async () => {
    try {
      await fetch('http://localhost:8000/authenticate', { method: 'POST' });
      fetchEmails();
    } catch (error) {
      console.error("Error logging in:", error);
    }
  };

  useEffect(() => {
    fetchEmails();
    const interval = setInterval(fetchEmails, 5000);
    return () => clearInterval(interval);
  }, []);


  const categories = ['IMPORTANT', 'UPDATES', 'SPAM'];

  return (
    <div className="grid-texture bg-surface-container-lowest text-on-surface font-body-md h-screen w-full overflow-hidden">
      <div className="app-layout w-full h-full" style={{ display: 'grid', gridTemplateColumns: '1fr', gridTemplateRows: '64px 1fr' }}>
        
        {/* Header */}
        <header className="app-header bg-surface-container-lowest/80 backdrop-blur-md border-b border-outline-variant/20 flex justify-between items-center px-8" style={{ gridColumn: 1 }}>
          <div className="flex items-center gap-4">
            <span className="text-headline-md font-headline-lg bg-clip-text text-transparent bg-gradient-to-r from-primary to-tertiary font-bold tracking-tight">
              MailMind Telemetry
            </span>
            <div className="flex items-center gap-2 bg-tertiary/10 border border-tertiary/20 rounded-full px-3 py-1">
              <span className="w-2 h-2 rounded-full bg-tertiary pulse-dot opacity-80"></span>
              <span className="text-[10px] font-bold text-tertiary uppercase tracking-wider">Autonomous Agent Online & Polling</span>
            </div>
            
            {isPolling && (
              <div className="flex items-center gap-2 bg-primary/10 border border-primary/20 rounded-full px-4 py-1.5 animate-pulse ml-4 shadow-[0_0_15px_rgba(34,211,238,0.2)]">
                <div className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin"></div>
                <span className="text-[10px] font-bold text-primary tracking-widest uppercase">AI Evaluating</span>
              </div>
            )}
          </div>
          <div className="flex items-center gap-4">
            <div className="relative">
              <div 
                className="flex items-center gap-3 cursor-pointer group"
                onClick={() => setShowUserMenu(!showUserMenu)}
              >
                <div className="flex flex-col items-end">
                  <span className="text-body-md font-medium text-on-surface group-hover:text-primary transition-colors">{userEmail}</span>
                  <span className="text-label-md text-on-surface-variant">System Operator</span>
                </div>
                <div className="w-10 h-10 rounded-full overflow-hidden border-2 border-surface-variant group-hover:border-primary transition-colors">
                  <img 
                    className="w-full h-full object-cover p-1 bg-surface-container-highest" 
                    alt="Profile" 
                    src={`https://api.dicebear.com/7.x/bottts/svg?seed=${encodeURIComponent(userEmail)}&backgroundColor=0c1324`}
                  />
                </div>
              </div>
              
              {/* User Dropdown Menu */}
              {showUserMenu && (
                <div className="absolute top-12 right-0 mt-2 w-56 bg-surface-container-highest rounded-xl py-2 z-50 shadow-2xl border border-outline-variant/30 animate-in fade-in slide-in-from-top-2 duration-200">
                  <div className="px-4 py-3 border-b border-outline-variant/20 mb-1">
                    <p className="text-sm font-medium text-on-surface">Account Settings</p>
                  </div>
                  <button 
                    onClick={handleSwitchAccount}
                    className="w-full text-left px-4 py-2.5 text-sm font-bold text-on-surface hover:bg-surface-variant/50 transition-colors flex items-center gap-2 cursor-pointer"
                  >
                    <span className="material-symbols-outlined text-[18px]">manage_accounts</span>
                    Switch Account
                  </button>
                  <button 
                    onClick={handleLogout}
                    className="w-full text-left px-4 py-2.5 text-sm font-bold text-error hover:bg-error/10 transition-colors flex items-center gap-2 cursor-pointer"
                  >
                    <span className="material-symbols-outlined text-[18px]">logout</span>
                    Logout
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        {/* Main Content */}
        <main className="app-main p-4 md:p-8 overflow-y-auto overflow-x-hidden relative" style={{ gridColumn: 1, gridRow: 2 }}>
          <div className="w-full max-w-[1600px] mx-auto relative z-10">
            
            {/* Dashboard Header Stats */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-10">
              <div className="glass-card p-6 rounded-2xl flex flex-col gap-3">
                <span className="text-on-surface-variant font-label-md text-label-md uppercase tracking-widest flex items-center gap-2">
                  <span className="material-symbols-outlined text-sm text-primary">timer</span>AI Processing Latency
                </span>
                <div className="flex items-baseline gap-2">
                  <span className="text-[32px] leading-tight font-headline-md text-on-surface font-bold">240ms</span>
                  <span className="text-tertiary text-[10px] font-bold uppercase tracking-wider bg-tertiary/10 px-1.5 py-0.5 rounded">Optimal</span>
                </div>
                <div className="h-1.5 bg-surface-variant rounded-full mt-auto overflow-hidden">
                  <div className="h-full bg-primary w-3/4 animate-pulse"></div>
                </div>
              </div>
              <div className="glass-card p-6 rounded-2xl flex flex-col gap-3">
                <span className="text-on-surface-variant font-label-md text-label-md uppercase tracking-widest flex items-center gap-2">
                  <span className="material-symbols-outlined text-sm text-tertiary">build</span>Human Corrections
                </span>
                <span className="text-[32px] leading-tight font-headline-md text-on-surface font-bold">
                  {emails.filter(e => e.human_label).length}
                </span>
                <span className="text-body-md font-body-md text-on-surface-variant">All Time Feedback</span>
                <div className="h-1.5 bg-surface-variant rounded-full mt-auto overflow-hidden">
                  <div className="h-full bg-tertiary w-[42%]"></div>
                </div>
              </div>
              <div className="glass-card p-6 rounded-2xl flex flex-col gap-3">
                <span className="text-on-surface-variant font-label-md text-label-md uppercase tracking-widest flex items-center gap-2">
                  <span className="material-symbols-outlined text-sm text-secondary">target</span>Total Processed
                </span>
                <span className="text-[32px] leading-tight font-headline-md text-on-surface font-bold">{emails.length}</span>
                <span className="text-body-md font-body-md text-on-surface-variant">Emails Evaluated</span>
                <div className="h-1.5 bg-surface-variant rounded-full mt-auto overflow-hidden">
                  <div className="h-full bg-secondary w-[99%]"></div>
                </div>
              </div>
              <div className="glass-card p-6 rounded-2xl flex flex-col gap-3">
                <span className="text-on-surface-variant font-label-md text-label-md uppercase tracking-widest flex items-center gap-2">
                  <span className="material-symbols-outlined text-sm text-error">hub</span>Active Polling Nodes
                </span>
                <span className="text-[32px] leading-tight font-headline-md text-on-surface font-bold">
                  2/2 <span className="text-body-md font-body-md text-tertiary">Online</span>
                </span>
                <span className="text-body-md font-body-md text-on-surface-variant">Cloud & Local LLMs</span>
                <div className="h-1.5 bg-surface-variant rounded-full mt-auto overflow-hidden">
                  <div className="h-full bg-error w-full"></div>
                </div>
              </div>
            </div>

            {/* Main Content Area */}
            {loading ? (
              <div className="flex flex-col items-center justify-center mt-32 animate-in fade-in duration-500">
                <div className="w-16 h-16 border-4 border-surface-variant border-t-primary rounded-full animate-spin mb-6 shadow-lg shadow-primary/20"></div>
                <h3 className="text-xl font-bold text-on-surface mb-2">Initializing Telemetry...</h3>
                <p className="text-on-surface-variant text-sm">Synchronizing with Cloud and Local AI Models</p>
              </div>
            ) : isLoggedOut ? (
              <div className="flex flex-col items-center justify-center mt-32 glass-card p-12 mx-auto max-w-2xl rounded-3xl border border-outline-variant/30 text-center animate-in fade-in slide-in-from-bottom-4 duration-500">
                <div className="w-20 h-20 bg-primary/10 rounded-full flex items-center justify-center mb-6 border border-primary/20">
                  <span className="material-symbols-outlined text-4xl text-primary">account_circle</span>
                </div>
                <h3 className="text-3xl font-headline-lg font-bold text-on-surface mb-4">MailMind is Disconnected</h3>
                <p className="text-on-surface-variant text-body-lg mb-8 max-w-md">
                  Your Google account has been completely disconnected. No emails are being routed or processed.
                </p>
                <button 
                  onClick={handleLogin}
                  className="bg-primary text-on-primary font-bold text-body-lg px-8 py-4 rounded-full hover:bg-primary-fixed hover:scale-105 active:scale-95 transition-all flex items-center gap-3 shadow-lg shadow-primary/20 cursor-pointer"
                >
                  <span className="material-symbols-outlined">login</span>
                  Connect Google Account
                </button>
              </div>
            ) : (
              /* Swimlanes Grid */
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                {categories.map(category => {
                  const categoryEmails = emails.filter(e => e.prediction === category);
                  
                  // Set theme per column based on the HTML provided
                  let iconStr, glowClass, textColorClass, borderColorClass, bgColorClass, blockLabel;
                  if (category === 'IMPORTANT') {
                    iconStr = 'analytics'; glowClass = 'glow-cyan'; textColorClass = 'text-primary'; borderColorClass = 'border-primary'; bgColorClass = 'bg-primary'; blockLabel = 'NEW';
                  } else if (category === 'UPDATES') {
                    iconStr = 'update'; glowClass = 'glow-blue'; textColorClass = 'text-secondary'; borderColorClass = 'border-secondary'; bgColorClass = 'bg-secondary'; blockLabel = 'NEW';
                  } else {
                    iconStr = 'security'; glowClass = 'glow-rose'; textColorClass = 'text-error'; borderColorClass = 'border-error'; bgColorClass = 'bg-error'; blockLabel = 'BLOCKED';
                  }

                  return (
                    <div key={category} className="flex flex-col gap-5">
                      <div className={`flex items-center justify-between pb-2 border-b ${borderColorClass}/20`}>
                        <div className="flex items-center gap-2">
                          <span className={`material-symbols-outlined ${textColorClass} ${glowClass}`}>{iconStr}</span>
                          <h2 className="text-headline-md font-headline-md text-on-surface font-bold">{category}</h2>
                        </div>
                        <span className={`${bgColorClass}/20 ${textColorClass} px-2 py-0.5 rounded text-mono-data font-mono-data border ${borderColorClass}/30 font-bold`}>
                          {categoryEmails.length} {blockLabel}
                        </span>
                      </div>

                      {categoryEmails.length === 0 ? (
                        <p className="text-on-surface-variant italic p-4 text-center text-sm">No recent emails in this category.</p>
                      ) : (
                        <div className="flex flex-col gap-5">
                          {categoryEmails.map(email => (
                            <EmailCard 
                              key={email.id} 
                              email={email} 
                              onFeedback={handleFeedback} 
                              theme={{ textColorClass, borderColorClass, bgColorClass }}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Atmospheric Backgrounds */}
          <div className="fixed top-1/4 left-1/4 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[120px] pointer-events-none mix-blend-screen z-0"></div>
          <div className="fixed bottom-1/4 right-1/4 w-[500px] h-[500px] bg-tertiary/5 rounded-full blur-[120px] pointer-events-none mix-blend-screen z-0"></div>
        </main>
      </div>
    </div>
  );
}

function EmailCard({ email, onFeedback, theme }) {
  const [showRejectMenu, setShowRejectMenu] = useState(false);
  const { textColorClass, borderColorClass, bgColorClass } = theme;

  const stripHtml = (html) => {
    if (!html) return "";
    const doc = new DOMParser().parseFromString(html, 'text/html');
    return doc.body.textContent || "";
  };

  const getBadgeColor = (label) => {
    switch (label) {
      case 'IMPORTANT': return 'bg-primary/10 text-primary border-primary/30';
      case 'UPDATES': return 'bg-secondary/10 text-secondary border-secondary/30';
      case 'SPAM': return 'bg-error/10 text-error border-error/30';
      case 'CONNECTION_FAILED': return 'bg-error/10 text-error border-error/30';
      default: return 'bg-surface-variant/50 text-on-surface-variant border-outline-variant/30';
    }
  };

  return (
    <div className={`email-card relative group ${email.prediction === 'SPAM' ? 'opacity-70 hover:opacity-100 transition-opacity' : ''}`}>
      <div className={`glass-card p-5 rounded-xl flex flex-col gap-4 ${email.prediction === 'SPAM' ? 'border-error/20 hover:border-error/40' : ''}`}>
        
        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 rounded-lg flex-shrink-0 overflow-hidden border border-outline-variant/50 bg-surface-container flex items-center justify-center font-bold text-lg text-on-surface-variant">
              {email.sender ? email.sender.charAt(0).toUpperCase() : '?'}
            </div>
            <div className="flex flex-col min-w-0 justify-center">
              <span className="text-body-md font-bold text-on-surface truncate" title={email.sender}>
                {email.sender ? email.sender.split('<')[0].trim() || email.sender : '?'}
              </span>
            </div>
          </div>
          
          {/* Dual Shadow Telemetry Badges */}
          <div className="flex flex-col items-end gap-1 flex-shrink-0">
            {email.local_prediction && email.prediction !== email.local_prediction ? (
              <>
                <div className="flex items-center gap-1.5" title="Cloud Prediction">
                  <span className="text-[9px] uppercase tracking-wider text-on-surface-variant font-bold">Cloud</span>
                  <span className={`px-2 py-0.5 text-[9px] font-bold rounded border block max-w-[70px] truncate ${getBadgeColor(email.prediction)}`}>
                    {email.prediction}
                  </span>
                </div>
                <div className="flex items-center gap-1.5" title="Local k-NN Prediction">
                  <span className="text-[9px] uppercase tracking-wider text-on-surface-variant font-bold">Local</span>
                  <span className={`px-2 py-0.5 text-[9px] font-bold rounded border block max-w-[70px] truncate ${getBadgeColor(email.local_prediction)}`}>
                    {email.local_prediction}
                  </span>
                </div>
              </>
            ) : (
              <div className="flex items-center gap-1.5" title="Cloud & Local predictions synced">
                {email.local_prediction && (
                  <svg className="w-3.5 h-3.5 text-tertiary drop-shadow-[0_0_5px_rgba(104,245,184,0.8)] flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M5 13l4 4L19 7"></path></svg>
                )}
                <span className={`px-3 py-1 text-[9px] font-bold uppercase tracking-tighter rounded-full border shadow-[0_0_10px_rgba(0,0,0,0.2)] block max-w-[90px] truncate ${getBadgeColor(email.prediction)}`}>
                  Synced: {email.prediction}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Content */}
        <div className="flex flex-col gap-1.5">
          <h3 className={`text-body-md font-bold text-on-surface truncate group-hover:${textColorClass} transition-colors`} title={email.subject}>
            {email.subject}
          </h3>
          <p className="text-label-md text-on-surface-variant line-clamp-2 leading-relaxed font-normal">
            {stripHtml(email.body)}
          </p>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-3 pt-3 border-t border-outline-variant/30">
          {email.human_label ? (
            <div className="w-full text-center text-sm text-tertiary font-bold flex items-center justify-center gap-2 py-2.5 rounded-lg bg-tertiary/10 border border-tertiary/20">
              <span className="material-symbols-outlined text-[18px]">verified</span> Feedback Logged ({email.human_label})
            </div>
          ) : (
            <>
              <button 
                onClick={() => onFeedback(email, email.prediction)}
                className="flex-1 flex items-center justify-center gap-1.5 py-2.5 rounded-lg bg-tertiary/10 text-tertiary text-label-md font-bold hover:bg-tertiary/20 border border-tertiary/20 hover:border-tertiary/40 transition-all active:scale-95 cursor-pointer"
              >
                <span className="material-symbols-outlined text-[18px]">check_circle</span> ACCEPT
              </button>
              <button 
                onClick={() => setShowRejectMenu(true)}
                className="flex-1 flex items-center justify-center gap-1.5 py-2.5 rounded-lg bg-error/10 text-error text-label-md font-bold hover:bg-error/20 border border-error/20 hover:border-error/40 transition-all active:scale-95 cursor-pointer"
              >
                <span className="material-symbols-outlined text-[18px]">cancel</span> REJECT
              </button>
            </>
          )}
        </div>
      </div>

      {/* Reject Sub-Menu */}
      {showRejectMenu && !email.human_label && (
        <div className="reject-menu absolute inset-0 z-10 glass-card rounded-xl p-4 flex flex-col justify-center items-center gap-4 bg-surface-container-lowest/95 backdrop-blur-xl border-error/40 shadow-2xl animate-in zoom-in-95 duration-200">
          <span className="text-label-md font-bold text-on-surface uppercase tracking-widest">Select correct label</span>
          <div className="flex flex-wrap justify-center gap-2">
            {['IMPORTANT', 'UPDATES', 'SPAM'].filter(l => l !== email.prediction).map(label => (
              <button
                key={label}
                onClick={() => {
                  onFeedback(email, label);
                  setShowRejectMenu(false);
                }}
                className={`px-4 py-2 rounded-lg text-label-md font-bold hover:scale-105 transition-transform duration-200 border cursor-pointer ${getBadgeColor(label)}`}
              >
                {label}
              </button>
            ))}
          </div>
          <button 
            onClick={() => setShowRejectMenu(false)}
            className="mt-2 text-on-surface-variant text-[11px] uppercase font-bold hover:text-on-surface tracking-wider cursor-pointer"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

export default App;
