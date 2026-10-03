// 仅处理用户选择或拖入的文件，不扫描磁盘。 / Handle only user-selected or dropped files.
const form=document.querySelector('#batch-upload');
if(form){
 const fileInput=form.querySelector('#files'), folder=form.querySelector('#folder'), drop=form.querySelector('#drop-zone');
 function files(){return [...fileInput.files,...folder.files];}
 function show(){form.querySelector('#file-selection').textContent='已选择 '+files().length+' 个文件';}
 fileInput.addEventListener('change',show);folder.addEventListener('change',show);
 drop.addEventListener('dragover',e=>{e.preventDefault();});
 drop.addEventListener('drop',e=>{e.preventDefault();fileInput.files=e.dataTransfer.files;show();});
 form.addEventListener('submit',e=>{
  if(!files().length){e.preventDefault();form.querySelector('#file-selection').textContent='请先选择资料。';return;}
  form.querySelector('[name=relative_paths]').value=JSON.stringify(files().map(f=>f.webkitRelativePath||f.name));
  const button=form.querySelector('#upload-button');button.disabled=true;button.textContent='正在本地归档与解析…';
 });
 window.addEventListener('pageshow',()=>{const button=form.querySelector('#upload-button');button.disabled=false;button.textContent='保存并解析这批资料';});
}

