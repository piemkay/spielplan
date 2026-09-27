<script>
  // Budget and Services still import this name; everything else imports Icon itself.
  import Icon from '$lib/components/Icon.svelte';

  let { name, size = 20 } = $props();
</script>

<Icon {name} {size} />
