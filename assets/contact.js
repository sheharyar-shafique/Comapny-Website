(()=>{
  const menu=document.getElementById('menu-btn');
  const links=document.getElementById('nav-links');
  if(menu&&links){menu.addEventListener('click',()=>{const open=links.classList.toggle('open');menu.setAttribute('aria-expanded',String(open))})}
  const form=document.querySelector('[data-aozal-form]');
  if(!form)return;
  const service=new URLSearchParams(window.location.search).get('service');
  const select=form.querySelector('[name="service"]');
  if(service&&select){
    const option=[...select.options].find(o=>o.value.toLowerCase()===service.toLowerCase());
    if(option)select.value=option.value;
    else if(service.length<=140){const extra=document.createElement('option');extra.value=service;extra.textContent=service;select.append(extra);select.value=service;}
  }
  const button=form.querySelector('button[type="submit"]');
  const status=form.querySelector('[role="status"]');
  const defaultHTML=button.innerHTML;
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(!form.reportValidity())return;
    const data=Object.fromEntries(new FormData(form).entries());
    button.disabled=true;
    button.textContent='Sending your message…';
    status.className='response';status.textContent='';
    try{
      if(location.protocol==='file:')throw new Error('Please open this website through the backend server to send messages.');
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),20000);
      let response;
      try{response=await fetch('/api/contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:controller.signal})}
      finally{clearTimeout(timeout)}
      let result;
      try{result=await response.json()}catch{throw new Error('The email service is unavailable. Please try the direct email address.');}
      if(!response.ok||!result.ok)throw new Error(result.error||'Unable to send message. Please try again.');
      form.reset();
      status.textContent='Success! Your message has been sent to the AOZAL team.';
      status.className='response visible';
    }catch(err){
      status.textContent=err.name==='AbortError'?'The request timed out. Please try again or email us directly.':err.message;
      status.className='response error visible';
    }finally{button.disabled=false;button.innerHTML=defaultHTML;}
  });
})();
